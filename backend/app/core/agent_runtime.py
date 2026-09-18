from datetime import datetime, timezone
from decimal import Decimal
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from backend.app.core.ai.base import ToolOutput
from backend.app.core.ai.cost_engine import ai_cost_engine
from backend.app.core.ai.engine import ai_engine
from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import reveal_config
from backend.app.core.error_safety import safe_error_label
from backend.app.core.module_access import company_module_enabled
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.company_profile import CompanyProfile
from backend.app.modules.ai_agent.models import (
    AIAgent,
    AIConversation,
    AIMessage,
    AIUsage,
)
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.channels.behavior import build_text_channel_behavior_prompt
from backend.app.modules.channels.catalog import N8N_CHANNEL_RUNTIME_ADAPTER, get_channel_capability
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.customer_ops.memory import build_customer_memory
from backend.app.modules.knowledge.service import knowledge_service
from backend.app.modules.tools.executor import tool_executor


GROUNDING_POLICY = """GROUNDING POLICY:
You are a professional employee of this business, not a generic chatbot.
Business facts must come only from COMPANY KNOWLEDGE or successful connected-tool results. This includes prices, menu items, services, products, hours, branches, addresses, policies, offers, availability, stock, delivery, booking rules, order status and contact details.
Never invent, estimate, autocomplete or infer a missing business fact. If the requested fact is unavailable, say only that you do not currently have that information and offer the next useful step or human assistance when appropriate.
Current COMPANY KNOWLEDGE overrides conflicting old conversation content.
Never claim a booking, order, cancellation, reschedule, payment or other action succeeded unless its tool returned success.

CONVERSATION POLICY:
Speak like a capable human-facing employee of the business, not like a generic assistant or scripted bot.
Respond to the customer's actual intent, not to every fact you know.
For the first simple greeting in a conversation, reply warmly and naturally, identify the business by its verified name when that name exists in COMPANY KNOWLEDGE, and ask one short useful question.
If CONVERSATION HISTORY or CUSTOMER MEMORY contains prior exchanges, this is not a first greeting. Never restart the conversation just because the current message is short, an acknowledgement, or an emoji; interpret it in the existing context.
Use CUSTOMER PROFILE and OLDER CUSTOMER INTERACTIONS for continuity and personalization only. Never treat them as authoritative business facts, and never mention internal memory, database, or profile mechanisms to the customer.
Never invent a business name. If no verified business name is available, give a natural greeting without naming the business.
A greeting must not advertise, list services, prices, menu items or offers unless the customer asks or they are directly necessary to answer the request.
If a message is incomplete or ambiguous, ask one short clarifying question. Do not guess and do not dump the catalog.
Do not repeat information already given unless needed for confirmation or the customer asks again.
Keep replies conversational and concise by default. Give the minimum information that fully answers the question, then ask at most one useful follow-up question when needed.
Do not expose internal terms such as tool, knowledge base, provider, model, prompt, database, handoff state or system configuration.
Do not say a capability is unavailable merely because a fact is absent from knowledge. Capability is determined by the tools made available to you.
For actions, collect only the missing required details progressively. Do not interrogate the customer with a long form in one message unless all details are naturally needed at once.
If a human transfer is required, state it naturally and use human_handoff when available; do not give a phone number as a substitute unless the business knowledge explicitly requires that contact method.
Match the customer's language and normal conversational register unless the configured employee instructions say otherwise.
Use BUSINESS CLOCK as the authoritative reference for current dates and times. Never invent a year for an incomplete customer date. For future-facing actions such as events, quotations, bookings or orders, do not silently resolve an incomplete date to a past date.
"""


_TOOL_INTENT_TERMS = (
    "book", "booking", "reserve", "reservation", "appointment", "order", "cancel",
    "reschedule", "refund", "payment", "pay", "quote", "quotation", "handoff",
    "human", "agent", "representative", "حجز", "احجز", "موعد", "طلب", "اطلب",
    "الغاء", "إلغاء", "تعديل", "استرجاع", "دفع", "ادفع", "عرض سعر", "موظف",
    "موظفة", "بشري", "انسان", "إنسان", "تواصل مع", "حولني", "حوّلني",
)


def _message_may_need_tools(message: str, history: str = "") -> bool:
    current = " ".join(str(message or "").lower().split())
    recent = " ".join(str(history or "")[-1200:].lower().split())
    combined = f"{recent} {current}".strip()
    return any(term in combined for term in _TOOL_INTENT_TERMS)


def _business_clock_context(
    timezone_name: str | None,
    now: datetime | None = None,
) -> str:
    zone_name = (timezone_name or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone_name = "UTC"
        zone = timezone.utc

    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    current = anchor.astimezone(zone)

    return (
        "BUSINESS CLOCK (authoritative for date/time interpretation):\n"
        f"Current business date: {current.date().isoformat()}\n"
        f"Current business time: {current.isoformat(timespec='seconds')}\n"
        f"Business timezone: {zone_name}\n"
        "When a customer gives a month and day without a year, resolve it to the "
        "next matching calendar date on or after the current business date unless "
        "the conversation clearly specifies another year or a past date. Never "
        "silently assign a past year to a future booking, event, quotation or "
        "operational request."
    )


class AgentRuntime:
    MAX_TOOL_ROUNDS = 6
    HISTORY_MAX_MESSAGES = 32
    HISTORY_MAX_CHARS = 8000
    HISTORY_ALLOWED_ROLES = {"user", "assistant", "human"}

    def assert_company_runtime_access(self, db, company_id: int) -> None:
        company = (
            db.query(Company)
            .filter(Company.id == company_id, Company.active.is_(True))
            .first()
        )
        if company is None:
            raise HTTPException(403, "Company is inactive or unavailable")

        module = (
            db.query(CompanyModule)
            .filter(
                CompanyModule.company_id == company_id,
                CompanyModule.module_name == "ai_agent",
                CompanyModule.enabled.is_(True),
            )
            .first()
        )
        if module is None:
            raise HTTPException(403, "AI Agent module is not enabled")

        if settings.is_production:
            service_limits.entitlement(db, company_id, "ai_agents")

    def get_agent(self, db, company_id: int, agent_id: int) -> AIAgent:
        agent = (
            db.query(AIAgent)
            .filter(AIAgent.id == agent_id, AIAgent.company_id == company_id)
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI Agent not found")
        if not agent.enabled:
            raise HTTPException(400, "AI Agent is disabled")
        if settings.is_production and agent.provider == "mock":
            raise HTTPException(503, "Mock AI provider is disabled in production")
        return agent

    def get_or_create_conversation(
        self,
        db,
        company_id: int,
        agent_id: int,
        conversation_id: int | None,
        message: str,
    ) -> AIConversation:
        if conversation_id is None:
            conversation = AIConversation(
                company_id=company_id,
                agent_id=agent_id,
                title=message[:200],
            )
            db.add(conversation)
            db.flush()
            return conversation

        conversation = (
            db.query(AIConversation)
            .populate_existing()
            .filter(
                AIConversation.id == conversation_id,
                AIConversation.company_id == company_id,
                AIConversation.agent_id == agent_id,
            )
            .first()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        return conversation

    def build_history(
        self,
        db,
        conversation_id: int,
        *,
        exclude_message_id: int | None = None,
    ) -> str:
        query = db.query(AIMessage).filter(
            AIMessage.conversation_id == conversation_id
        )
        if exclude_message_id is not None:
            query = query.filter(AIMessage.id != exclude_message_id)
        messages = (
            query.order_by(AIMessage.id.desc())
            .limit(self.HISTORY_MAX_MESSAGES)
            .all()
        )

        selected = []
        used_chars = 0
        for item in messages:
            role = str(item.role or "").strip().lower()
            content = str(item.content or "").strip()
            if role not in self.HISTORY_ALLOWED_ROLES or not content:
                continue
            line = f"{role}: {content}"
            line_size = len(line) + (1 if selected else 0)
            if selected and used_chars + line_size > self.HISTORY_MAX_CHARS:
                break
            if not selected and line_size > self.HISTORY_MAX_CHARS:
                line = line[: self.HISTORY_MAX_CHARS]
                line_size = len(line)
            selected.append(line)
            used_chars += line_size

        selected.reverse()
        return "\n".join(selected)

    def build_business_clock(self, db, company_id: int) -> str:
        profile = (
            db.query(CompanyProfile)
            .filter(CompanyProfile.company_id == company_id)
            .first()
        )
        return _business_clock_context(profile.timezone if profile else None)

    def build_runtime_system_prompt(
        self,
        db,
        agent: AIAgent,
        conversation: AIConversation,
    ) -> str:
        base = str(agent.system_prompt or "").strip()
        channel_type = str(conversation.channel_type or "").strip().lower()
        if not channel_type or conversation.channel_id is None:
            return base
        capability = get_channel_capability(channel_type) or {}
        if (
            channel_type != "whatsapp"
            and capability.get("runtime_adapter") != N8N_CHANNEL_RUNTIME_ADAPTER
        ):
            return base
        channel_type_filter = (
            AgentChannel.channel_type == "whatsapp"
            if channel_type == "whatsapp"
            else AgentChannel.channel_type == channel_type
        )
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == conversation.company_id,
                AgentChannel.id == conversation.channel_id,
                AgentChannel.agent_id == conversation.agent_id,
                channel_type_filter,
                AgentChannel.enabled.is_(True),
            )
            .first()
        )
        if channel is None:
            return base
        behavior = build_text_channel_behavior_prompt(
            channel_type,
            reveal_config(channel.config) or {},
        )
        return (base + "\n\n" + behavior).strip()

    def _record_failed_request(
        self,
        db,
        *,
        company_id: int,
        agent_id: int,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        provider_cost: Decimal,
        error_message: str,
        latency_ms: int,
        routing_attempts: list[dict],
        end_to_end_latency_ms: int | None = None,
        stage_timings_ms: dict | None = None,
    ) -> None:
        db.add(
            AIUsage(
                company_id=company_id,
                agent_id=agent_id,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                provider_cost=provider_cost,
                status="failed",
                error_message=error_message,
                latency_ms=latency_ms,
            )
        )
        audit_service.log(
            db=db,
            company_id=company_id,
            action="agent.chat_failed",
            resource_type="ai_agent",
            resource_id=agent_id,
            details={
                "provider": provider,
                "model": model,
                "error": error_message,
                "latency_ms": latency_ms,
                "end_to_end_latency_ms": end_to_end_latency_ms,
                "stage_timings_ms": stage_timings_ms or {},
                "routing_attempts": routing_attempts,
            },
        )

    def chat(
        self,
        db,
        company_id: int,
        agent_id: int,
        message: str,
        conversation_id: int | None = None,
        commit: bool = True,
        allow_tools: bool = True,
        channel_type: str | None = None,
        channel_id: int | None = None,
        external_contact_id: str | None = None,
        system_prompt_override: str | None = None,
        user_message_source_key: str | None = None,
    ) -> dict:
        message = (message or "").strip()
        if not message:
            raise HTTPException(400, "Message is required")
        if len(message) > 12000:
            raise HTTPException(413, "Message is too long")

        source_key = str(user_message_source_key or "").strip() or None
        if source_key and len(source_key) > 320:
            raise HTTPException(400, "Message source identity is too long")

        end_to_end_started_at = perf_counter()
        stage_timings_ms: dict[str, int] = {}

        stage_started = perf_counter()
        self.assert_company_runtime_access(db, company_id)
        limits_service.check_token_limit(db, company_id)
        agent = self.get_agent(db, company_id, agent_id)
        stage_timings_ms["access_and_limits"] = int(
            (perf_counter() - stage_started) * 1000
        )

        stage_started = perf_counter()
        try:
            selections = runtime_selections(
                db,
                company_id,
                agent.provider,
                agent.model,
                message=message,
            )
        except ValueError as exc:
            raise HTTPException(503, safe_error_label(exc)) from exc
        if not selections:
            raise HTTPException(503, "No eligible AI provider/model is available")
        stage_timings_ms["routing"] = int((perf_counter() - stage_started) * 1000)

        active_provider = selections[0].provider
        active_model = selections[0].model

        stage_started = perf_counter()
        conversation = self.get_or_create_conversation(
            db, company_id, agent.id, conversation_id, message
        )
        if channel_type:
            from backend.app.modules.channels.conversation_source import (
                bind_conversation_source,
            )

            try:
                bind_conversation_source(
                    db,
                    conversation_id=conversation.id,
                    company_id=company_id,
                    agent_id=agent.id,
                    channel_type=channel_type,
                    channel_id=channel_id,
                    external_contact_id=external_contact_id,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        existing_user_message = None
        if source_key:
            existing_user_message = (
                db.query(AIMessage)
                .filter(
                    AIMessage.source_key == source_key,
                    AIMessage.conversation_id == conversation.id,
                    AIMessage.role == "user",
                )
                .first()
            )
            if existing_user_message is not None and (
                str(existing_user_message.content or "").strip() != message
            ):
                raise HTTPException(
                    409,
                    "Message source identity is already bound to different content",
                )

        system_prompt = (
            system_prompt_override
            if system_prompt_override is not None
            else self.build_runtime_system_prompt(db, agent, conversation)
        )
        history = self.build_history(
            db,
            conversation.id,
            exclude_message_id=(
                existing_user_message.id if existing_user_message is not None else None
            ),
        )
        customer_memory = build_customer_memory(db, conversation)
        business_clock = self.build_business_clock(db, company_id)
        stage_timings_ms["conversation_context"] = int(
            (perf_counter() - stage_started) * 1000
        )

        stage_started = perf_counter()
        knowledge = ""
        if company_module_enabled(db, company_id, "knowledge"):
            knowledge = knowledge_service.get_agent_context(
                db, company_id, agent.id, message
            )
        stage_timings_ms["knowledge"] = int((perf_counter() - stage_started) * 1000)

        stage_started = perf_counter()
        available_tools = []
        tools_relevant = allow_tools and _message_may_need_tools(message, history)
        if tools_relevant and company_module_enabled(db, company_id, "tools"):
            available_tools = tool_executor.get_agent_tools(
                db=db,
                agent_id=agent.id,
            )
        tool_definitions = [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool.get(
                    "input_schema",
                    {"type": "object", "properties": {}},
                ),
            }
            for tool in available_tools
        ]
        stage_timings_ms["tool_discovery"] = int(
            (perf_counter() - stage_started) * 1000
        )

        context_parts = [GROUNDING_POLICY, business_clock]
        if knowledge:
            context_parts.append(
                "COMPANY KNOWLEDGE (authoritative facts; use only when relevant "
                "to the customer's current intent):\n" + knowledge
            )
        else:
            context_parts.append(
                "COMPANY KNOWLEDGE:\nNo relevant business knowledge was retrieved "
                "for this message. Do not invent business facts."
            )
        if customer_memory:
            context_parts.append(
                "CUSTOMER MEMORY (persistent continuity; use only when relevant):\n"
                + customer_memory
            )
        if history:
            context_parts.append(
                "CONVERSATION HISTORY (use for continuity; not authoritative for "
                "business facts):\n" + history
            )
        context_parts.append(
            "CURRENT CUSTOMER MESSAGE (answer this intent directly):\n" + message
        )
        runtime_message = "\n\n".join(context_parts)

        if existing_user_message is not None:
            user_message = existing_user_message
        else:
            user_message = AIMessage(
                conversation_id=conversation.id,
                role="user",
                content=message,
                source_key=source_key,
            )
            db.add(user_message)
            db.flush()

        tool_outputs = None
        continuation = None
        executed_tools = []
        routing_attempts = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_provider_cost = Decimal("0")
        final_text = ""
        provider_started_at = perf_counter()
        total_model_ms = 0
        total_tool_execution_ms = 0

        for round_index in range(self.MAX_TOOL_ROUNDS):
            try:
                if round_index == 0:
                    result = None
                    last_error = None
                    for selection in selections:
                        active_provider = selection.provider
                        active_model = selection.model
                        attempt_started = perf_counter()
                        try:
                            result = ai_engine.generate(
                                provider_name=active_provider,
                                system_prompt=system_prompt,
                                user_message=runtime_message,
                                model=active_model,
                                tools=tool_definitions,
                                tool_outputs=None,
                                continuation=None,
                            )
                            attempt_ms = int(
                                (perf_counter() - attempt_started) * 1000
                            )
                            total_model_ms += attempt_ms
                            routing_attempts.append(
                                {
                                    "provider": active_provider,
                                    "model": active_model,
                                    "reason": selection.reason,
                                    "success": True,
                                    "latency_ms": attempt_ms,
                                }
                            )
                            break
                        except Exception as exc:
                            attempt_ms = int(
                                (perf_counter() - attempt_started) * 1000
                            )
                            total_model_ms += attempt_ms
                            last_error = exc
                            routing_attempts.append(
                                {
                                    "provider": active_provider,
                                    "model": active_model,
                                    "reason": selection.reason,
                                    "success": False,
                                    "latency_ms": attempt_ms,
                                    "error": safe_error_label(exc),
                                }
                            )
                    if result is None:
                        raise last_error or RuntimeError(
                            "No AI provider completed the request"
                        )
                else:
                    attempt_started = perf_counter()
                    result = ai_engine.generate(
                        provider_name=active_provider,
                        system_prompt=system_prompt,
                        user_message=runtime_message,
                        model=active_model,
                        tools=tool_definitions,
                        tool_outputs=tool_outputs,
                        continuation=continuation,
                    )
                    attempt_ms = int(
                        (perf_counter() - attempt_started) * 1000
                    )
                    total_model_ms += attempt_ms
            except Exception as exc:
                provider_latency_ms = int(
                    (perf_counter() - provider_started_at) * 1000
                )
                stage_timings_ms["model"] = total_model_ms
                stage_timings_ms["tool_execution"] = total_tool_execution_ms
                end_to_end_latency_ms = int(
                    (perf_counter() - end_to_end_started_at) * 1000
                )
                error_message = safe_error_label(exc)
                if commit:
                    db.rollback()
                    self._record_failed_request(
                        db,
                        company_id=company_id,
                        agent_id=agent_id,
                        provider=active_provider,
                        model=active_model,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        total_tokens=total_tokens,
                        provider_cost=total_provider_cost,
                        error_message=error_message,
                        latency_ms=provider_latency_ms,
                        routing_attempts=routing_attempts,
                        end_to_end_latency_ms=end_to_end_latency_ms,
                        stage_timings_ms=stage_timings_ms,
                    )
                    db.commit()
                raise HTTPException(502, "AI provider request failed") from exc

            total_input_tokens += result.input_tokens
            total_output_tokens += result.output_tokens
            total_tokens += result.total_tokens
            calculated_cost = ai_cost_engine.calculate(
                db=db,
                provider_name=active_provider,
                model_name=active_model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            total_provider_cost += (
                result.cost
                if result.cost and result.cost > Decimal("0")
                else calculated_cost
            )

            if not result.tool_calls:
                final_text = result.text
                break

            if not allow_tools or not tool_definitions:
                raise HTTPException(
                    500,
                    "AI attempted a tool call in a tool-disabled runtime context",
                )

            continuation = result.continuation
            tool_outputs = []
            for call in result.tool_calls:
                tool_started = perf_counter()
                execution = tool_executor.execute(
                    db=db,
                    company_id=company_id,
                    agent_id=agent.id,
                    tool_name=call.name,
                    arguments=call.arguments or {},
                    conversation_id=conversation.id,
                )
                tool_latency_ms = int(
                    (perf_counter() - tool_started) * 1000
                )
                total_tool_execution_ms += tool_latency_ms
                output = ToolOutput(
                    call_id=call.id,
                    name=call.name,
                    success=bool(execution.get("success")),
                    data=execution.get("data"),
                    error=execution.get("error"),
                )
                tool_outputs.append(output)
                executed_tools.append(
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments or {},
                        "success": output.success,
                        "data": output.data,
                        "error": output.error,
                        "latency_ms": tool_latency_ms,
                    }
                )
                audit_service.log(
                    db=db,
                    company_id=company_id,
                    action="agent.tool_executed",
                    resource_type="ai_agent",
                    resource_id=agent.id,
                    details={
                        "conversation_id": conversation.id,
                        "tool": call.name,
                        "success": output.success,
                        "error": output.error,
                        "latency_ms": tool_latency_ms,
                    },
                )
        else:
            raise HTTPException(
                500,
                "Agent exceeded the maximum number of tool execution rounds",
            )

        if not final_text:
            final_text = (
                "The agent completed its actions but did not return a final response."
            )

        stage_timings_ms["model"] = total_model_ms
        stage_timings_ms["tool_execution"] = total_tool_execution_ms
        provider_latency_ms = int(
            (perf_counter() - provider_started_at) * 1000
        )

        persistence_started = perf_counter()
        assistant_message = AIMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=final_text,
        )
        db.add(assistant_message)
        usage = AIUsage(
            company_id=company_id,
            agent_id=agent.id,
            provider=active_provider,
            model=active_model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            total_tokens=total_tokens,
            provider_cost=total_provider_cost,
            status="success",
            latency_ms=provider_latency_ms,
        )
        if settings.is_production and total_tokens > 0:
            service_limits.check(
                db,
                company_id,
                "ai_agents",
                "tokens",
                quantity=total_tokens,
            )

        db.add(usage)
        db.flush()
        stage_timings_ms["persistence_before_commit"] = int(
            (perf_counter() - persistence_started) * 1000
        )
        end_to_end_latency_ms = int(
            (perf_counter() - end_to_end_started_at) * 1000
        )

        audit_service.log(
            db=db,
            company_id=company_id,
            action="agent.chat_completed",
            resource_type="ai_agent",
            resource_id=agent.id,
            details={
                "conversation_id": conversation.id,
                "provider": active_provider,
                "model": active_model,
                "tool_execution_count": len(executed_tools),
                "tool_schemas_sent": len(tool_definitions),
                "total_tokens": total_tokens,
                "latency_ms": usage.latency_ms,
                "end_to_end_latency_ms": end_to_end_latency_ms,
                "stage_timings_ms": stage_timings_ms,
                "routing_attempts": routing_attempts,
            },
        )

        if commit:
            commit_started = perf_counter()
            db.commit()
            db.refresh(user_message)
            db.refresh(assistant_message)
            stage_timings_ms["commit_and_refresh"] = int(
                (perf_counter() - commit_started) * 1000
            )
            end_to_end_latency_ms = int(
                (perf_counter() - end_to_end_started_at) * 1000
            )

        return {
            "conversation_id": conversation.id,
            "agent_id": agent.id,
            "company_id": company_id,
            "provider": active_provider,
            "model": active_model,
            "message": {
                "id": user_message.id,
                "role": user_message.role,
                "content": user_message.content,
                "source_key": user_message.source_key,
            },
            "response": {
                "id": assistant_message.id,
                "role": assistant_message.role,
                "content": assistant_message.content,
            },
            "tool_executions": executed_tools,
            "routing_attempts": routing_attempts,
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "provider_cost": total_provider_cost,
                "latency_ms": usage.latency_ms,
                "end_to_end_latency_ms": end_to_end_latency_ms,
                "stage_timings_ms": stage_timings_ms,
            },
        }


agent_runtime = AgentRuntime()
