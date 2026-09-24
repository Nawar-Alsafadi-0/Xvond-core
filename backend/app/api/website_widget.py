import json
import secrets
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from backend.app.api.admin_channels import (
    _ensure_channels_module,
    _has_real_runtime_provider,
)
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager, require_xvond_admin
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.self_service_policy import (
    assert_self_service_channel_selected,
    is_self_service_company,
)
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.channels.catalog import validate_channel_config
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument

router = APIRouter(tags=["Website AI Widget"])


class WebsiteSetup(BaseModel):
    allowed_domain: str
    widget_name: str | None = None
    welcome_message: str | None = None
    welcome_message_en: str | None = None
    position: str = "right"
    custom_instructions: str | None = None
    accent_color: str = "#111827"
    launcher_label: str | None = None
    launcher_label_ar: str | None = None
    launcher_label_en: str | None = None
    human_assistance_mode: Literal["direct_handoff", "contact_only", "ai_only"] = "direct_handoff"
    contact_phone: str | None = None
    contact_whatsapp: str | None = None
    contact_email: str | None = None
    contact_url: str | None = None


DEFAULT_BEHAVIOR = """WEBSITE CHANNEL CONTEXT:
You are speaking with a visitor through the business website chat widget.
The website is only the communication channel. Your business identity, knowledge and configured actions are shared with the same AI employee across channels.
Be concise, natural and useful. Do not dump services, prices or menus unless relevant to the visitor's request.
If the request is vague, ask one short clarifying question.
Use clean plain text for normal chat replies. Avoid markdown bold markers, headings and decorative formatting unless the visitor explicitly asks for formatted text.
Never invent business facts and never claim an action succeeded unless its configured action returned success.
Do not mention AI providers, prompts, tools, databases, routing or Xvond internals.
"""


def _embed(channel_id: int) -> str:
    src = f"/channels/website/{channel_id}/widget.js"
    if settings.PUBLIC_BASE_URL:
        src = settings.PUBLIC_BASE_URL + src
    return f'<script src="{src}" async></script>'


def _normalized_domain(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(400, "Allowed domain is required")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Allowed domain is invalid")
    if parsed.username or parsed.password:
        raise HTTPException(400, "Allowed domain must not contain credentials")
    host = parsed.hostname.lower().strip(".")
    if host in {"localhost", "127.0.0.1", "::1"} and settings.is_production:
        raise HTTPException(400, "Localhost cannot be used as a production website domain")
    return host


def _assistance_mode(config: dict) -> str:
    value = str(config.get("human_assistance_mode") or "direct_handoff").strip().lower()
    if value not in {"direct_handoff", "contact_only", "ai_only"}:
        return "direct_handoff"
    return value


def website_contact_methods(config: dict) -> list[str]:
    values = [
        ("Phone", config.get("contact_phone")),
        ("WhatsApp", config.get("contact_whatsapp")),
        ("Email", config.get("contact_email")),
        ("Contact URL", config.get("contact_url")),
    ]
    return [f"{label}: {str(value).strip()}" for label, value in values if str(value or "").strip()]


def _useful_business_knowledge(document: KnowledgeDocument) -> bool:
    content = (document.content or "").strip()
    if len(content) < 20:
        return False
    if document.source_type != "business_profile":
        return True
    markers = (
        "Description:",
        "Working Hours:",
        "Locations / Branches:",
        "Services:",
        "Policies:",
        "Business Rules:",
    )
    return len(content) >= 80 and any(marker in content for marker in markers)


def _ready(db, channel: AgentChannel) -> list[str]:
    blockers = []
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == channel.agent_id,
            AIAgent.company_id == channel.company_id,
            AIAgent.enabled.is_(True),
        )
        .first()
    )
    if not agent:
        blockers.append("AI employee must be active")
    elif not _has_real_runtime_provider(db, channel.company_id, agent):
        blockers.append("At least one real AI provider/model must be configured")

    if not settings.is_test:
        try:
            service_limits.entitlement(db, channel.company_id, "ai_agents")
        except HTTPException:
            blockers.append("An active AI Agents service subscription is required")

    config = reveal_config(channel.config) or {}
    if not str(config.get("allowed_domain") or "").strip():
        blockers.append("Allowed website domain is required")
    if not str(config.get("widget_key") or "").strip():
        blockers.append("Widget key is missing")
    if _assistance_mode(config) == "contact_only" and not website_contact_methods(config):
        blockers.append("Add at least one contact method when Human Assistance is Contact Only")
    if settings.is_production and not settings.PUBLIC_BASE_URL:
        blockers.append("Xvond public API URL is not configured")

    docs = (
        db.query(KnowledgeDocument)
        .join(AgentKnowledge, AgentKnowledge.document_id == KnowledgeDocument.id)
        .filter(
            KnowledgeDocument.company_id == channel.company_id,
            KnowledgeDocument.enabled.is_(True),
            AgentKnowledge.agent_id == channel.agent_id,
            AgentKnowledge.enabled.is_(True),
        )
        .all()
    )
    if not any(_useful_business_knowledge(doc) for doc in docs):
        blockers.append("Add real business knowledge before activating Website Chat")
    return blockers


def _website_config_from_setup(
    *,
    agent: AIAgent,
    data: WebsiteSetup,
    previous: dict | None = None,
) -> dict:
    previous = dict(previous or {})
    legacy_label = (data.launcher_label or "").strip()
    label_ar = (data.launcher_label_ar or legacy_label or "مساعد Xvond").strip()[:30]
    label_en = (data.launcher_label_en or legacy_label or "Chat").strip()[:30]
    config = {
        "allowed_domain": _normalized_domain(data.allowed_domain),
        "widget_name": (data.widget_name or agent.name).strip()[:120],
        "welcome_message": (
            data.welcome_message or "مرحباً، كيف يمكنني مساعدتك؟"
        ).strip()[:1000],
        "welcome_message_en": (
            data.welcome_message_en or "Hello, how can I help you?"
        ).strip()[:1000],
        "position": "left" if data.position == "left" else "right",
        "custom_instructions": (data.custom_instructions or "").strip()[:4000],
        "accent_color": (
            data.accent_color
            if data.accent_color.startswith("#") and len(data.accent_color) in {4, 7}
            else "#111827"
        ),
        "launcher_label": label_en,
        "launcher_label_ar": label_ar,
        "launcher_label_en": label_en,
        "human_assistance_mode": data.human_assistance_mode,
        "contact_phone": (data.contact_phone or "").strip()[:120],
        "contact_whatsapp": (data.contact_whatsapp or "").strip()[:120],
        "contact_email": (data.contact_email or "").strip()[:254],
        "contact_url": (data.contact_url or "").strip()[:1000],
        "widget_key": previous.get("widget_key") or secrets.token_urlsafe(32),
    }
    if isinstance(previous.get("employee_setup"), dict):
        config["employee_setup"] = previous["employee_setup"]
    return config


_CUSTOMER_WEBSITE_FIELDS = {
    "allowed_domain",
    "widget_name",
    "welcome_message",
    "welcome_message_en",
    "position",
    "custom_instructions",
    "accent_color",
    "launcher_label",
    "launcher_label_ar",
    "launcher_label_en",
    "human_assistance_mode",
    "contact_phone",
    "contact_whatsapp",
    "contact_email",
    "contact_url",
}


def _customer_website_config(config: dict) -> dict:
    return {
        key: value
        for key, value in (config or {}).items()
        if key in _CUSTOMER_WEBSITE_FIELDS
    }


def _website_prepared(config: dict) -> bool:
    try:
        validate_channel_config("website", config or {})
    except ValueError:
        return False
    return bool(str((config or {}).get("widget_key") or "").strip())


def _customer_self_service_agent(
    db,
    *,
    current_user: User,
    agent_id: int,
) -> tuple[Company, AIAgent]:
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == current_user.company_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(404, "AI employee not found")
    company = db.query(Company).filter(Company.id == current_user.company_id).first()
    if is_self_service_company(company):
        assert_self_service_channel_selected(
            db,
            company=company,
            agent=agent,
            channel_type="website",
        )
    return company, agent


def _behavior(config: dict) -> str:
    custom = str(config.get("custom_instructions") or "").strip()
    mode = _assistance_mode(config)
    contacts = website_contact_methods(config)
    parts = [DEFAULT_BEHAVIOR]
    if mode == "direct_handoff":
        parts.append(
            "HUMAN ASSISTANCE POLICY: Direct handoff is enabled for this website channel. "
            "If the visitor explicitly asks for a human and the human_handoff action is available, use it. "
            "Never claim a transfer happened unless the action succeeds."
        )
    elif mode == "contact_only":
        parts.append(
            "HUMAN ASSISTANCE POLICY: Contact Only. Never create or claim a live human handoff in this website chat. "
            "If the visitor asks to speak with a human, give only the configured contact methods below and keep the AI conversation available."
        )
        parts.append("CONFIGURED HUMAN CONTACT METHODS (authoritative for this channel):\n" + "\n".join(contacts))
    else:
        parts.append(
            "HUMAN ASSISTANCE POLICY: AI Only. Do not offer, create or claim a human handoff and do not invent contact details. "
            "If the visitor asks for a human, explain briefly that direct human assistance is not available in this chat and continue helping within your configured capabilities."
        )
    if custom:
        parts.append("Website-specific instructions: " + custom)
    return "\n".join(parts)


@router.get("/customer/website-channel/agents/{agent_id}")
def customer_get_website_config(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company, agent = _customer_self_service_agent(
            db,
            current_user=current_user,
            agent_id=agent_id,
        )
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "website",
            )
            .first()
        )
        if channel is None:
            if not is_self_service_company(company):
                raise HTTPException(404, "Website Chat is not assigned to this AI employee")
            return {
                "agent_id": agent.id,
                "configured": False,
                "prepared": False,
                "enabled": False,
                "can_edit": not agent.enabled,
                "config": {},
                "embed_code": None,
            }
        plain = reveal_config(channel.config) or {}
        prepared = _website_prepared(plain)
        return {
            "agent_id": agent.id,
            "channel_id": channel.id,
            "configured": prepared,
            "prepared": prepared,
            "enabled": bool(channel.enabled),
            "can_edit": not agent.enabled,
            "config": _customer_website_config(plain),
            "embed_code": _embed(channel.id) if prepared else None,
        }
    finally:
        db.close()


@router.put("/customer/website-channel/agents/{agent_id}")
def customer_configure_website(
    agent_id: int,
    data: WebsiteSetup,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company, agent = _customer_self_service_agent(
            db,
            current_user=current_user,
            agent_id=agent_id,
        )
        if agent.enabled:
            raise HTTPException(
                409,
                "Deactivate this employee before changing Website Chat setup",
            )

        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "website",
            )
            .with_for_update()
            .first()
        )
        if channel is None and not is_self_service_company(company):
            raise HTTPException(404, "Website Chat is not assigned to this AI employee")
        old = reveal_config(channel.config) if channel is not None else {}
        config = _website_config_from_setup(
            agent=agent,
            data=data,
            previous=old,
        )
        if channel is None:
            channel = AgentChannel(
                company_id=current_user.company_id,
                agent_id=agent.id,
                channel_type="website",
                config=config,
                enabled=False,
            )
            db.add(channel)
        else:
            channel.config = merge_config(channel.config, config)
            channel.enabled = False

        _ensure_channels_module(db, current_user.company_id)
        db.commit()
        db.refresh(channel)
        plain = reveal_config(channel.config) or {}
        return {
            "status": "configured",
            "agent_id": agent.id,
            "channel_id": channel.id,
            "configured": _website_prepared(plain),
            "prepared": _website_prepared(plain),
            "enabled": False,
            "can_edit": True,
            "config": _customer_website_config(plain),
            "embed_code": _embed(channel.id),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/admin/website-channel/agents/{agent_id}")
def get_config(
    agent_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.agent_id == agent_id,
                AgentChannel.channel_type == "website",
            )
            .first()
        )
        if not channel:
            return {"configured": False}
        config = reveal_config(channel.config) or {}
        blockers = _ready(db, channel)
        safe = {key: value for key, value in config.items() if key != "widget_key"}
        return {
            "configured": True,
            "channel_id": channel.id,
            "enabled": channel.enabled,
            "ready": not blockers,
            "blockers": blockers,
            "config": safe,
            "embed_code": _embed(channel.id),
        }
    finally:
        db.close()


@router.put("/admin/website-channel/agents/{agent_id}")
def configure(
    agent_id: int,
    data: WebsiteSetup,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "AI employee not found")
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.agent_id == agent_id,
                AgentChannel.channel_type == "website",
            )
            .first()
        )
        old = reveal_config(channel.config) if channel is not None else {}
        config = _website_config_from_setup(
            agent=agent,
            data=data,
            previous=old,
        )
        if channel:
            channel.config = merge_config(channel.config, config)
            channel.enabled = False
        else:
            channel = AgentChannel(
                company_id=agent.company_id,
                agent_id=agent.id,
                channel_type="website",
                config=config,
                enabled=False,
            )
            db.add(channel)
        _ensure_channels_module(db, agent.company_id)
        db.commit()
        db.refresh(channel)
        blockers = _ready(db, channel)
        return {
            "status": "configured",
            "channel_id": channel.id,
            "ready": not blockers,
            "blockers": blockers,
            "embed_code": _embed(channel.id),
        }
    finally:
        db.close()


@router.post("/admin/website-channel/{channel_id}/activate")
def activate(
    channel_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.id == channel_id,
                AgentChannel.channel_type == "website",
            )
            .first()
        )
        if not channel:
            raise HTTPException(404, "Website channel not found")
        blockers = _ready(db, channel)
        if blockers:
            raise HTTPException(
                409, "Website Chat is not ready: " + "; ".join(blockers)
            )
        if not channel.enabled:
            limits_service.check_channel_limit(db, channel.company_id)
            channel.enabled = True
        db.commit()
        return {"status": "active", "channel_id": channel.id}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/admin/website-channel/{channel_id}/deactivate")
def deactivate(
    channel_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.id == channel_id,
                AgentChannel.channel_type == "website",
            )
            .first()
        )
        if not channel:
            raise HTTPException(404, "Website channel not found")
        channel.enabled = False
        db.commit()
        return {"status": "inactive"}
    finally:
        db.close()


@router.get("/channels/website/{channel_id}/widget.js")
def widget_js(channel_id: int):
    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.id == channel_id,
                AgentChannel.channel_type == "website",
                AgentChannel.enabled.is_(True),
            )
            .first()
        )
        if not channel:
            raise HTTPException(404, "Widget unavailable")
        config = reveal_config(channel.config) or {}
        key = config.get("widget_key")
        name = config.get("widget_name") or "AI Assistant"
        welcome_ar = config.get("welcome_message") or "مرحباً، كيف يمكنني مساعدتك؟"
        welcome_en = config.get("welcome_message_en") or "Hello, how can I help you?"
        position = config.get("position") or "right"
        accent = config.get("accent_color") or "#111827"
        label_ar = config.get("launcher_label_ar") or config.get("launcher_label") or "مساعد Xvond"
        label_en = config.get("launcher_label_en") or config.get("launcher_label") or "Chat"

        template = r'''(()=>{
if(window.__xvondWidget)return;
window.__xvondWidget=1;
const API=new URL(document.currentScript.src).origin;
const CHANNEL=__CHANNEL__;
const WIDGET_KEY=__WIDGET_KEY__;
const CID_KEY='xvond_conversation_'+CHANNEL;
const TOKEN_KEY='xvond_visitor_token_'+CHANNEL;
const HISTORY_KEY='xvond_history_'+CHANNEL;
const HISTORY_LIMIT=100;
let cid=localStorage.getItem(CID_KEY)||null;
let visitorToken=localStorage.getItem(TOKEN_KEY)||null;
let lastId=0;
let typingNode=null;
let history=[];
let renderedIds=new Set();
let pollInFlight=false;
function clearHistoryCache(){history=[];renderedIds.clear();lastId=0;localStorage.removeItem(HISTORY_KEY);}
function loadHistoryCache(){
  if(!cid||!visitorToken)return false;
  try{
    const raw=localStorage.getItem(HISTORY_KEY);
    if(!raw)return false;
    const cached=JSON.parse(raw);
    if(String(cached?.conversationId||'')!==String(cid)||!Array.isArray(cached?.messages)){localStorage.removeItem(HISTORY_KEY);return false;}
    history=cached.messages.slice(-HISTORY_LIMIT).filter(m=>m&&m.id&&m.content&&['user','assistant','human'].includes(m.role));
    lastId=history.reduce((max,m)=>Math.max(max,Number(m.id)||0),0);
    return history.length>0;
  }catch(_e){localStorage.removeItem(HISTORY_KEY);return false;}
}
function saveHistoryCache(){
  if(!cid||!visitorToken)return;
  try{localStorage.setItem(HISTORY_KEY,JSON.stringify({conversationId:String(cid),messages:history.slice(-HISTORY_LIMIT)}));}catch(_e){}
}
if(cid&&!visitorToken){localStorage.removeItem(CID_KEY);localStorage.removeItem(HISTORY_KEY);cid=null;}
if(visitorToken&&!cid){localStorage.removeItem(TOKEN_KEY);localStorage.removeItem(HISTORY_KEY);visitorToken=null;}
const side=__POSITION__;
const accent=__ACCENT__;
function isArabicUI(){
  const pageLang=(document.documentElement.lang||'').toLowerCase();
  const pageDir=(document.documentElement.dir||getComputedStyle(document.documentElement).direction||'').toLowerCase();
  return pageLang.startsWith('ar')||pageDir==='rtl';
}
let arabicUI=isArabicUI();
let welcome=arabicUI?__WELCOME_AR__:__WELCOME_EN__;
let launcherLabel=arabicUI?__LABEL_AR__:__LABEL_EN__;
let inputPlaceholder=arabicUI?'اكتب رسالتك':'Type your message';
let sendLabel=arabicUI?'إرسال':'Send';
let failureMessage=arabicUI?'تعذر إرسال الرسالة الآن. حاول مرة أخرى.':'Unable to send your message right now. Please try again.';
function refreshLocaleState(){
  arabicUI=isArabicUI();
  welcome=arabicUI?__WELCOME_AR__:__WELCOME_EN__;
  launcherLabel=arabicUI?__LABEL_AR__:__LABEL_EN__;
  inputPlaceholder=arabicUI?'اكتب رسالتك':'Type your message';
  sendLabel=arabicUI?'إرسال':'Send';
  failureMessage=arabicUI?'تعذر إرسال الرسالة الآن. حاول مرة أخرى.':'Unable to send your message right now. Please try again.';
}
function requestHeaders(jsonBody=true){const h={'X-Xvond-Widget-Key':WIDGET_KEY};if(jsonBody)h['Content-Type']='application/json';if(visitorToken)h['X-Xvond-Visitor-Token']=visitorToken;return h;}
function cleanText(value){return String(value||'').replace(/\*\*(.*?)\*\*/gs,'$1').replace(/__(.*?)__/gs,'$1').replace(/`([^`]+)`/g,'$1').replace(/^#{1,6}\s+/gm,'').trim();}
const css=`#xvond-btn{position:fixed;bottom:20px;${side}:20px;z-index:2147483646;border:0;border-radius:999px;padding:14px 18px;cursor:pointer;box-shadow:0 8px 30px #0003;background:${accent};color:#fff;font:600 14px Arial,sans-serif}#xvond-box{position:fixed;bottom:78px;${side}:20px;width:min(380px,calc(100vw - 24px));height:520px;max-height:70vh;background:#fff;color:#111;z-index:2147483647;border-radius:18px;box-shadow:0 18px 60px #0004;display:none;overflow:hidden;font-family:Arial,sans-serif}#xvond-head{padding:16px;font-weight:700;border-bottom:1px solid #eee;direction:auto;text-align:start}#xvond-msgs{height:calc(100% - 118px);overflow:auto;overflow-x:hidden;padding:14px;scroll-behavior:smooth}.xvond-m{margin:8px 0;padding:10px 12px;border-radius:12px;white-space:pre-wrap;line-height:1.55;unicode-bidi:plaintext;overflow-wrap:anywhere;word-break:normal}.xvond-m[dir="rtl"]{text-align:right}.xvond-m[dir="ltr"]{text-align:left}.xvond-u{background:#eef3ff;margin-inline-start:40px}.xvond-a{background:#f5f5f5;margin-inline-end:40px}.xvond-typing{width:max-content;min-width:52px;padding:11px 14px}.xvond-dot{display:inline-block;width:7px;height:7px;margin:0 2px;border-radius:50%;background:#7b8190;animation:xvondTyping 1.15s infinite ease-in-out}.xvond-dot:nth-child(2){animation-delay:.15s}.xvond-dot:nth-child(3){animation-delay:.3s}@keyframes xvondTyping{0%,60%,100%{transform:translateY(0);opacity:.45}30%{transform:translateY(-4px);opacity:1}}#xvond-form{display:flex;border-top:1px solid #eee;padding:10px;gap:8px}#xvond-in{flex:1;min-width:0;border:1px solid #ddd;border-radius:10px;padding:10px;direction:auto;text-align:start;unicode-bidi:plaintext}#xvond-send{border:0;border-radius:10px;padding:10px 14px;cursor:pointer;background:${accent};color:#fff}#xvond-send:disabled,#xvond-in:disabled{opacity:.65;cursor:not-allowed}@media(max-width:480px){#xvond-btn{bottom:14px;${side}:14px}#xvond-box{bottom:70px;${side}:12px;width:calc(100vw - 24px);height:min(540px,72vh);border-radius:16px}.xvond-u{margin-inline-start:24px}.xvond-a{margin-inline-end:24px}}`;
const st=document.createElement('style');st.textContent=css;document.head.appendChild(st);
const btn=document.createElement('button');btn.id='xvond-btn';btn.textContent=launcherLabel;btn.setAttribute('dir','auto');
const box=document.createElement('div');box.id='xvond-box';box.setAttribute('dir',arabicUI?'rtl':'ltr');box.innerHTML=`<div id="xvond-head"></div><div id="xvond-msgs"></div><form id="xvond-form"><input id="xvond-in" autocomplete="off"><button id="xvond-send" type="submit"></button></form>`;
document.body.append(btn,box);box.querySelector('#xvond-head').textContent=__NAME__;box.querySelector('#xvond-in').placeholder=inputPlaceholder;box.querySelector('#xvond-send').textContent=sendLabel;
let welcomeNode=null;
function applyLocale(){
  refreshLocaleState();
  btn.textContent=launcherLabel;
  box.setAttribute('dir',arabicUI?'rtl':'ltr');
  box.querySelector('#xvond-in').placeholder=inputPlaceholder;
  box.querySelector('#xvond-send').textContent=sendLabel;
  if(welcomeNode&&welcomeNode.isConnected)welcomeNode.textContent=welcome;
}
if('MutationObserver'in window){
  const localeObserver=new MutationObserver(()=>applyLocale());
  localeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['lang','dir']});
}
const msgs=box.querySelector('#xvond-msgs');
function scrollToLatest(){msgs.scrollTop=msgs.scrollHeight;requestAnimationFrame(()=>{msgs.scrollTop=msgs.scrollHeight;});}
function add(t,c,id=null){const text=cleanText(t);if(!text)return null;if(id&&renderedIds.has(String(id)))return null;const d=document.createElement('div');d.className='xvond-m '+c;d.setAttribute('dir','auto');if(id){d.dataset.messageId=String(id);renderedIds.add(String(id));}d.textContent=text;msgs.appendChild(d);scrollToLatest();return d;}
function renderMessage(m){if(!m)return;if(m.role==='user')add(m.content,'xvond-u',m.id);else if(m.role==='assistant'||m.role==='human')add(m.content,'xvond-a',m.id);}
function cacheMessage(m){const id=Number(m?.id)||0;const role=String(m?.role||'');const content=String(m?.content||'');if(!id||!content||!['user','assistant','human'].includes(role))return;const existing=history.findIndex(x=>Number(x.id)===id);const item={id,role,content};if(existing>=0)history[existing]=item;else history.push(item);history.sort((a,b)=>Number(a.id)-Number(b.id));history=history.slice(-HISTORY_LIMIT);remember(id);saveHistoryCache();}
function syncMessage(m,render=true){cacheMessage(m);if(render)renderMessage(m);}
function restoreCachedHistory(){if(!loadHistoryCache())return false;for(const m of history)renderMessage(m);return true;}
function showTyping(){hideTyping();const d=document.createElement('div');d.className='xvond-m xvond-a xvond-typing';d.setAttribute('aria-label',arabicUI?'المساعد يكتب الآن':'Assistant is typing');d.innerHTML='<span class="xvond-dot"></span><span class="xvond-dot"></span><span class="xvond-dot"></span>';typingNode=d;msgs.appendChild(d);scrollToLatest();}
function hideTyping(){if(typingNode){typingNode.remove();typingNode=null;}}
function remember(id){if(id&&id>lastId)lastId=id;}
function rememberSession(data){if(data.conversation_id){const nextCid=String(data.conversation_id);if(cid&&cid!==nextCid)clearHistoryCache();cid=nextCid;localStorage.setItem(CID_KEY,cid);}if(data.visitor_token){visitorToken=data.visitor_token;localStorage.setItem(TOKEN_KEY,visitorToken);}}
restoreCachedHistory();
if(!cid||!visitorToken)welcomeNode=add(welcome,'xvond-a');
btn.onclick=()=>{const opening=box.style.display!=='block';box.style.display=opening?'block':'none';if(opening){scrollToLatest();poll();}};
box.querySelector('#xvond-form').onsubmit=async e=>{e.preventDefault();const input=box.querySelector('#xvond-in');const send=box.querySelector('#xvond-send');const m=input.value.trim();if(!m||send.disabled)return;input.value='';add(m,'xvond-u');showTyping();send.disabled=true;input.disabled=true;try{const r=await fetch(API+'/channels/website/'+CHANNEL+'/chat',{method:'POST',headers:requestHeaders(true),body:JSON.stringify({message:m,conversation_id:cid?Number(cid):null})});const j=await r.json();if(!r.ok)throw new Error(j.detail||'Request failed');rememberSession(j);if(j.message)syncMessage(j.message,false);hideTyping();if(j.response)syncMessage(j.response,true);}catch(_e){hideTyping();add(failureMessage,'xvond-a');}finally{send.disabled=false;input.disabled=false;input.focus();}};
async function poll(){if(!cid||!visitorToken||pollInFlight||document.hidden)return;pollInFlight=true;const restoring=lastId===0;try{const r=await fetch(API+'/channels/website/'+CHANNEL+'/conversation/'+cid+'/messages?after_id='+lastId,{headers:requestHeaders(false)});if(r.status===401||r.status===404){localStorage.removeItem(CID_KEY);localStorage.removeItem(TOKEN_KEY);localStorage.removeItem(HISTORY_KEY);cid=null;visitorToken=null;clearHistoryCache();msgs.innerHTML='';welcomeNode=add(welcome,'xvond-a');return;}if(!r.ok)return;const j=await r.json();for(const m of (j.messages||[])){if(m.role==='human')hideTyping();syncMessage(m,true);}if(restoring&&!(j.messages||[]).length&&!msgs.children.length)add(welcome,'xvond-a');}catch(_e){}finally{pollInFlight=false;}}
poll();
setInterval(()=>{if(!document.hidden)poll();},2500);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
})();'''
        replacements = {
            "__CHANNEL__": str(channel_id),
            "__WIDGET_KEY__": json.dumps(key, ensure_ascii=False),
            "__POSITION__": json.dumps(position, ensure_ascii=False),
            "__ACCENT__": json.dumps(accent, ensure_ascii=False),
            "__LABEL_AR__": json.dumps(label_ar, ensure_ascii=False),
            "__LABEL_EN__": json.dumps(label_en, ensure_ascii=False),
            "__NAME__": json.dumps(name, ensure_ascii=False),
            "__WELCOME_AR__": json.dumps(welcome_ar, ensure_ascii=False),
            "__WELCOME_EN__": json.dumps(welcome_en, ensure_ascii=False),
        }
        js = template
        for marker, value in replacements.items():
            js = js.replace(marker, value)
        return Response(
            content=js,
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=60, stale-while-revalidate=300"},
        )
    finally:
        db.close()


def website_behavior(config: dict) -> str:
    return _behavior(config)