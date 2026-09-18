from pathlib import Path

from backend.app.modules.ai_agent.employee_capability_builder import (
    build_managed_action_config,
)
from backend.app.modules.ai_agent.employee_compiler import (
    build_compiled_employee_system_prompt,
    build_compiler_user_message,
    parse_compiler_response,
)


ROOT = Path(__file__).resolve().parents[1]


def test_compiler_turns_unknown_digital_requirement_into_xvond_build_work():
    job_brief = "راقب منصة متخصصة خاصة فيني ونفذ قاعدة مخصصة عليها كل يوم"
    response = """{
      "role": "Personal monitoring agent",
      "scope": "personal",
      "summary": "Monitor a specialist platform and apply a custom rule.",
      "tasks": [{"name":"Monitor","description":"Watch the platform","trigger":"daily"}],
      "requirements": [
        {"key":"specialist_platform_monitor","kind":"custom","purpose":"Read the platform and apply the rule","requires_connection":false,"primitives":["http_api","workflow_engine"],"execution_plan":[{"id":"fetch","op":"http_get_json","url_field":"url"},{"id":"value","op":"extract","source":"fetch","path":"value"},{"id":"matched","op":"compare","source":"value","operator":"gte","value_field":"threshold"},{"id":"notify","op":"notify","when":"matched","title":"Monitor alert","message":"Condition matched."}]},
        {"key":"scheduling","kind":"automation","purpose":"Run daily","requires_connection":false,"primitives":["scheduler","workflow_engine"]}
      ],
      "permissions": [{"action":"send a notification","mode":"automatic"}],
      "setup_questions": []
    }"""
    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["job_brief"] == job_brief
    novel = next(x for x in spec["requirements"] if x["key"] == "specialist_platform_monitor")
    assert novel["known_to_xvond"] is False
    assert novel["status"] == "xvond_build"
    assert "workflow_engine" in novel["primitives"]
    assert [step["op"] for step in novel["execution_plan"]] == [
        "http_get_json",
        "extract",
        "compare",
        "notify",
    ]
    scheduling = next(x for x in spec["requirements"] if x["key"] == "scheduling")
    assert scheduling["known_to_xvond"] is True
    assert scheduling["status"] == "xvond_build"
    assert spec["unsupported_requirements"] == []


def test_unknown_external_account_requirement_asks_for_connection_instead_of_rejection():
    response = """{
      "role": "Private account assistant",
      "scope": "personal",
      "summary": "Watch a private account.",
      "tasks": [],
      "requirements": [
        {"key":"private_vendor_account","kind":"integration","purpose":"Read account data","requires_connection":true,"customer_inputs":["Connect the vendor account"],"primitives":["http_api","workflow_engine"]}
      ],
      "permissions": [],
      "setup_questions": ["Connect the vendor account"]
    }"""
    spec = parse_compiler_response(response, job_brief="راقب حسابي الخاص")
    requirement = spec["requirements"][0]

    assert requirement["status"] == "connection_required"
    assert requirement["delivery_mode"] == "connect_and_compose"
    assert requirement["customer_inputs"] == ["Connect the vendor account"]
    assert spec["unsupported_requirements"] == []


def test_compiler_maps_email_and_instagram_to_real_connection_requirements():
    response = """{
      "role": "Content and inbox employee",
      "scope": "business",
      "summary": "Read email and publish approved content.",
      "tasks": [],
      "requirements": [
        {"key":"email_read","kind":"integration","purpose":"Read inbox"},
        {"key":"email_send","kind":"integration","purpose":"Reply to email"},
        {"key":"instagram_publish","kind":"integration","purpose":"Publish content"}
      ],
      "permissions": [{"action":"publish content","mode":"ask_before"}],
      "setup_questions": []
    }"""
    spec = parse_compiler_response(response, job_brief="اقرأ الإيميل وانشر المحتوى")

    statuses = {x["key"]: x["status"] for x in spec["requirements"]}
    assert statuses["email_read"] == "connection_required"
    assert statuses["email_send"] == "connection_required"
    assert statuses["instagram_publish"] == "connection_required"


def test_managed_capability_compiles_to_generic_workflow_action():
    spec = {
        "job_brief": "راقب الأسعار من https://prices.example.com وابعتلي تنبيه",
        "summary": "Monitor prices and notify the owner.",
        "permissions": [{"action": "send a notification", "mode": "automatic"}],
    }
    requirement = {
        "key": "competitor_price_monitor",
        "purpose": "Monitor competitor prices and send a notification",
        "primitives": ["http_api", "scheduler", "workflow_engine"],
        "execution_plan": [
            {"id": "fetch", "op": "http_get_json", "url_field": "url"},
            {"id": "price", "op": "extract", "source": "fetch", "path": "price"},
            {"id": "matched", "op": "compare", "source": "price", "operator": "lte", "value_field": "target_price"},
            {"id": "notify", "op": "notify", "when": "matched", "title": "Price alert", "message": "Target reached."},
        ],
    }
    action = build_managed_action_config(requirement=requirement, spec=spec)

    assert action["destination"]["type"] == "xvond_internal"
    assert action["destination"]["adapter"] == "generic_capability"
    assert action["destination"]["capability_key"] == "competitor_price_monitor"
    assert action["destination"]["delivery_mode"] == "compose"
    assert "workflow_engine" in action["destination"]["primitives"]
    assert action["destination"]["allowed_hosts"] == ["prices.example.com"]
    assert action["destination"]["execution_plan"][0]["op"] == "http_get_json"
    assert action["xvond_generated"] is True


def test_compiler_drops_free_form_or_unknown_runtime_ops():
    response = """{
      "role": "Safe worker",
      "scope": "personal",
      "summary": "Do a safe task.",
      "tasks": [],
      "requirements": [
        {
          "key":"safe_task",
          "kind":"custom",
          "purpose":"Do a safe task",
          "execution_plan":[
            {"id":"hack","op":"shell","code":"rm -rf /"},
            {"id":"notify","op":"notify","title":"Done","message":"Finished"}
          ]
        }
      ],
      "permissions": [],
      "setup_questions": []
    }"""
    spec = parse_compiler_response(response, job_brief="نفذ مهمة آمنة")
    assert spec["requirements"][0]["execution_plan"] == [
        {
            "id": "notify",
            "op": "notify",
            "title": "Done",
            "message": "Finished",
        }
    ]


def test_compiler_prompt_keeps_full_job_and_selected_channels():
    message = build_compiler_user_message(
        job_brief="بدي موظف يرد عالعملاء ويحجز وينشر محتوى تلقائي",
        requested_channels=["whatsapp", "instagram"],
    )
    assert "يرد عالعملاء ويحجز وينشر محتوى تلقائي" in message
    assert "whatsapp, instagram" in message


def test_compiled_runtime_prompt_keeps_job_permissions_and_missing_setup_honest():
    spec = {
        "role": "Inbox employee",
        "scope": "business",
        "job_brief": "اقرأ الإيميلات ورد بعد موافقتي",
        "summary": "Read and reply to email.",
        "tasks": [{"name": "Inbox", "description": "Read new email", "trigger": "new email"}],
        "requirements": [
            {"key": "email_read", "status": "connection_required", "purpose": "Read inbox"}
        ],
        "permissions": [{"action": "send email", "mode": "ask_before"}],
    }
    prompt = build_compiled_employee_system_prompt(owner_name="Xvond Demo", spec=spec)
    assert "اقرأ الإيميلات ورد بعد موافقتي" in prompt
    assert "send email: ask_before" in prompt
    assert "email_read: connection_required" in prompt
    assert "Do not claim an external action succeeded" in prompt


def test_paid_compile_endpoint_is_part_of_customer_employee_builder_contract():
    source = (ROOT / "backend" / "app" / "api" / "customer_employee_builder.py").read_text(
        encoding="utf-8"
    )
    assert '@router.post("/{agent_id}/compile")' in source
    assert 'service_limits.entitlement(db, current_user.company_id, "ai_agents")' in source
    assert "_compile_employee_spec" in source
    assert 'builder["compiled_spec"] = compiled_spec' in source
    assert 'builder["missing_information"] = list(compiled_spec.get("setup_required") or [])' in source
    assert "agent.system_prompt = build_compiled_employee_system_prompt" in source
    assert "provision_compiled_capabilities" in source


def test_customer_portal_shows_xvond_owned_build_instead_of_unsupported_features():
    source = (ROOT / "frontend" / "customer" / "employee-builder.js").read_text(
        encoding="utf-8"
    )
    assert "Build employee" in source
    assert "EMPLOYEE BUILD PLAN" in source
    assert "/compile`" in source
    assert "Xvond builds this" in source
    assert "custom required" not in source.lower()


def test_compiler_contract_has_no_custom_required_end_state():
    source = (ROOT / "backend" / "app" / "modules" / "ai_agent" / "employee_compiler.py").read_text(
        encoding="utf-8"
    )
    assert '"xvond_build"' in source
    assert '"unsupported_requirements": []' in source
    assert "custom_required" not in source



def test_compiler_normalizes_explicit_schedule_and_only_grounded_runtime_inputs():
    job_brief = (
        "راقب https://prices.example.com كل يوم الساعة 8 "
        "ونبهني تلقائيا إذا وصلت القيمة 100"
    )
    response = """{
      "role": "Price monitor",
      "scope": "personal",
      "summary": "Monitor a price endpoint.",
      "tasks": [{"name":"Monitor","description":"Watch price","trigger":"daily at 8"}],
      "requirements": [{
        "key":"price_monitor",
        "kind":"custom",
        "purpose":"Monitor price",
        "primitives":["http_api","scheduler","workflow_engine"],
        "schedule":{"kind":"daily","hour":8,"minute":0,"source_text":"كل يوم الساعة 8"},
        "runtime_inputs":{
          "url":"https://prices.example.com",
          "threshold":100,
          "hallucinated":"not in brief"
        },
        "execution_plan":[
          {"id":"fetch","op":"http_get_json","url_field":"url"},
          {"id":"value","op":"extract","source":"fetch","path":"value"},
          {"id":"matched","op":"compare","source":"value","operator":"gte","value_field":"threshold"},
          {"id":"notify","op":"notify","when":"matched"}
        ]
      }],
      "permissions":[{"action":"Monitor price","mode":"automatic"}],
      "setup_questions":[]
    }"""
    spec = parse_compiler_response(response, job_brief=job_brief)
    requirement = spec["requirements"][0]

    assert requirement["schedule"] == {"kind": "daily", "hour": 8, "minute": 0, "source_text": "كل يوم الساعة 8"}
    assert requirement["runtime_inputs"] == {
        "url": "https://prices.example.com",
        "threshold": 100,
    }
    assert "scheduler" in requirement["primitives"]
    assert "workflow_engine" in requirement["primitives"]



def test_compiler_rejects_schedule_not_grounded_in_job_brief():
    response = """{
      "role":"Monitor",
      "scope":"personal",
      "summary":"Monitor data.",
      "tasks":[],
      "requirements":[{
        "key":"monitor",
        "kind":"custom",
        "purpose":"Monitor data",
        "schedule":{
          "kind":"daily",
          "hour":3,
          "minute":0,
          "source_text":"every day at 3"
        }
      }],
      "permissions":[],
      "setup_questions":[]
    }"""
    spec = parse_compiler_response(response, job_brief="راقب البيانات وأخبرني عند التغيير")
    assert spec["requirements"][0]["schedule"] is None
