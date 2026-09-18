from pathlib import Path

from backend.app.modules.ai_agent.employee_capability_builder import (
    build_internal_booking_action_config,
    build_internal_record_action_config,
    build_managed_action_config,
)
from backend.app.modules.ai_agent.employee_compiler import (
    build_compiled_employee_system_prompt,
    build_compiler_user_message,
    is_sensitive_requirement_key,
    normalize_compiled_spec,
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
    assert "build_employee" in source
    assert "BUILD PROGRESS" in source
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



def test_compiler_never_persists_secret_runtime_inputs_even_if_grounded():
    job_brief = "Check https://example.com every 30 minutes using api_key secret123."
    response = """{
      "role":"Monitor",
      "scope":"personal",
      "summary":"Monitor data.",
      "tasks":[],
      "requirements":[{
        "key":"monitor",
        "kind":"custom",
        "purpose":"Monitor data",
        "primitives":["http_api","scheduler","workflow_engine"],
        "schedule":{"kind":"interval","every_minutes":30,"source_text":"every 30 minutes"},
        "runtime_inputs":{
          "url":"https://example.com",
          "api_key":"secret123"
        },
        "execution_plan":[{"id":"fetch","op":"http_get_json","url_field":"url"}]
      }],
      "permissions":[{"action":"Monitor data","mode":"automatic"}],
      "setup_questions":[]
    }"""
    spec = parse_compiler_response(response, job_brief=job_brief)
    assert spec["requirements"][0]["runtime_inputs"] == {
        "url": "https://example.com"
    }


def test_customer_portal_labels_ready_scheduled_work_truthfully():
    source = (ROOT / "frontend" / "customer" / "employee-builder.js").read_text(
        encoding="utf-8"
    )
    assert "scheduled & ready" in source
    assert "automatic permission required" in source
    assert "schedule setup required" in source


def test_builder_generic_setup_never_treats_credentials_as_plain_data():
    for key in (
        "password",
        "api_key",
        "crm_access_token",
        "oauth_client_secret",
        "vendor_credentials",
        "aws_access_key",
    ):
        assert is_sensitive_requirement_key(key) is True

    for key in ("workspace_id", "timezone", "target_market", "account_context"):
        assert is_sensitive_requirement_key(key) is False


def test_compiler_smart_intake_keeps_known_facts_and_asks_only_for_missing_required_fields():
    job_brief = "بدي موظف حجوزات لمطعم بيت الشام يرد على واتساب"
    response = """{
      "role": "Restaurant booking employee",
      "scope": "business",
      "summary": "Handle restaurant reservations.",
      "intake": {
        "known": [
          {"key":"business_name","label":"اسم المطعم","value":"بيت الشام"},
          {"key":"invented_city","label":"المدينة","value":"مسقط"}
        ],
        "missing": [
          {"key":"business_name","label":"اسم المطعم","purpose":"Identify the restaurant"},
          {"key":"working_hours","label":"أوقات الدوام","purpose":"Know when bookings can be accepted"},
          {"key":"booking_capacity","label":"سعة الحجز","purpose":"Avoid overbooking"}
        ]
      },
      "tasks": [{"name":"Reservations","description":"Handle bookings","trigger":"customer request"}],
      "requirements": [
        {"key":"booking","kind":"module","purpose":"Create and manage reservations"},
        {"key":"whatsapp","kind":"channel","purpose":"Talk with customers"}
      ],
      "permissions": [{"action":"booking","mode":"ask_before"}],
      "setup_questions": []
    }"""
    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["intake"]["known"] == [
        {"key": "business_name", "label": "اسم المطعم", "value": "بيت الشام"}
    ]
    assert [item["key"] for item in spec["intake"]["missing"]] == [
        "working_hours",
        "booking_capacity",
    ]
    booking = next(x for x in spec["requirements"] if x["key"] == "booking")
    assert booking["fulfillment_mode"] == "xvond_internal"
    assert booking["status"] == "customer_input_required"
    assert set(booking["customer_inputs"]) == {
        "working_days",
        "opening_time",
        "closing_time",
        "slot_minutes",
        "capacity",
    }
    assert "employee_context" not in {
        item["key"] for item in spec["requirements"]
    }
    assert spec["setup_required"] == ["booking", "whatsapp"]


def test_compiled_runtime_prompt_includes_grounded_job_brief_context():
    spec = {
        "role": "Restaurant employee",
        "scope": "business",
        "job_brief": "مطعم بيت الشام",
        "summary": "Help restaurant customers.",
        "tasks": [],
        "requirements": [],
        "permissions": [],
        "intake": {
            "known": [
                {"key": "business_name", "label": "اسم المطعم", "value": "بيت الشام"}
            ],
            "missing": [],
        },
    }
    prompt = build_compiled_employee_system_prompt(owner_name="Workspace", spec=spec)
    assert "KNOWN CONTEXT EXTRACTED FROM THE JOB BRIEF" in prompt
    assert "اسم المطعم: بيت الشام" in prompt


def test_booking_defaults_to_xvond_internal_and_asks_only_for_missing_schedule_facts():
    response = """{
      "role":"Booking employee",
      "scope":"business",
      "summary":"Handle appointments.",
      "tasks":[{"name":"Appointments","description":"Book appointments","trigger":"customer request"}],
      "requirements":[{
        "key":"booking",
        "kind":"module",
        "purpose":"Create and manage appointments",
        "requires_connection":false,
        "fulfillment_mode":"xvond_internal",
        "runtime_inputs":{"opening_time":"09:00"},
        "customer_inputs":[]
      }],
      "permissions":[{"action":"booking","mode":"ask_before"}],
      "setup_questions":[]
    }"""
    spec = parse_compiler_response(
        response,
        job_brief="بدي موظف يحجز مواعيد، الدوام بيفتح الساعة 09:00",
    )
    booking = next(item for item in spec["requirements"] if item["key"] == "booking")

    assert booking["fulfillment_mode"] == "xvond_internal"
    assert booking["status"] == "customer_input_required"
    assert booking["after_input_status"] == "xvond_build"
    assert booking["runtime_inputs"]["opening_time"] == "09:00"
    assert set(booking["customer_inputs"]) == {
        "working_days",
        "closing_time",
        "slot_minutes",
    }


def test_booking_explicit_external_system_stays_connection_required():
    response = """{
      "role":"Booking employee",
      "scope":"business",
      "summary":"Use the clinic's existing booking system.",
      "tasks":[],
      "requirements":[{
        "key":"booking",
        "kind":"integration",
        "purpose":"Use Acme Scheduler for appointments",
        "requires_connection":true,
        "fulfillment_mode":"external_connection",
        "customer_inputs":[]
      }],
      "permissions":[{"action":"booking","mode":"ask_before"}],
      "setup_questions":[]
    }"""
    spec = parse_compiler_response(
        response,
        job_brief="اربط الموظف مع نظام Acme Scheduler الموجود عندي للحجوزات",
    )
    booking = spec["requirements"][0]
    assert booking["status"] == "connection_required"
    assert booking["fulfillment_mode"] == "external_connection"


def test_internal_booking_action_builds_real_schedule_from_customer_setup():
    spec = {
        "permissions": [{"action": "booking", "mode": "ask_before"}],
        "customer_inputs": {
            "booking": {
                "working_days": "Sunday-Thursday",
                "opening_time": "9:00 am",
                "closing_time": "5:30 pm",
                "slot_minutes": "30 minutes",
            }
        },
    }
    requirement = {
        "key": "booking",
        "purpose": "Book appointments",
        "fulfillment_mode": "xvond_internal",
        "runtime_inputs": {},
    }

    action = build_internal_booking_action_config(requirement=requirement, spec=spec)

    assert action["destination"]["type"] == "xvond_internal"
    assert action["destination"]["adapter"] == "booking"
    assert action["availability"]["mode"] == "xvond_schedule"
    assert action["availability"]["schedule"] == {
        "weekdays": [0, 1, 2, 3, 6],
        "start": "09:00",
        "end": "17:30",
        "slot_minutes": 30,
        "capacity": 1,
    }
    assert action["_xvond_booking_setup_ready"] is True


def test_xvond_native_business_records_are_real_portal_capabilities():
    spec = {
        "permissions": [{"action": "lead_management", "mode": "automatic"}],
    }
    requirement = {
        "key": "lead_management",
        "purpose": "Capture and follow up sales leads",
        "fulfillment_mode": "xvond_internal",
    }
    action = build_internal_record_action_config(requirement=requirement, spec=spec)

    assert action["module"] == "lead_management"
    assert action["destination"]["type"] == "xvond_internal"
    assert action["destination"]["adapter"] == "business_record"
    assert action["destination"]["record_type"] == "lead_management"
    assert any(field["key"] == "interest" and field["required"] for field in action["fields"])
    assert action["confirmation_required"] is False


def test_external_booking_action_keeps_real_integration_and_provider_endpoints():
    spec = {"permissions": [{"action": "booking", "mode": "ask_before"}]}
    requirement = {
        "key": "booking",
        "purpose": "Use existing booking system",
        "fulfillment_mode": "external_connection",
        "integration_id": 41,
        "validation_required": True,
        "integration_operations": {
            "availability": {"method": "POST", "endpoint": "/availability"},
            "execute": {"method": "POST", "endpoint": "/bookings"},
        },
    }
    action = build_managed_action_config(requirement=requirement, spec=spec)

    assert action["module"] == "booking"
    assert action["destination"]["type"] == "integration"
    assert action["destination"]["integration_id"] == 41
    assert action["destination"]["validation_required"] is True
    assert action["destination"]["operations"]["execute"]["endpoint"] == "/bookings"
    assert action["availability"]["mode"] == "integration"



def test_content_generation_is_native_and_does_not_create_fake_execution_setup():
    spec = normalize_compiled_spec(
        {
            "role": "Content assistant",
            "scope": "personal",
            "requirements": [
                {
                    "key": "content_generation",
                    "kind": "tool",
                    "purpose": "Draft social content on demand",
                }
            ],
        },
        job_brief="Draft social content for me inside Xvond.",
    )

    requirement = spec["requirements"][0]
    assert requirement["key"] == "content_generation"
    assert requirement["status"] == "available"
    assert requirement["delivery_mode"] == "native"
    assert requirement["primitives"] == ["content_generation"]
    assert requirement["execution_plan"] == []
    assert "content_generation" in spec["ready_requirements"]
    assert "content_generation" not in spec["build_required"]



def test_instagram_publish_includes_media_generation_primitive():
    spec = normalize_compiled_spec(
        {
            "role": "Social publisher",
            "requirements": [
                {
                    "key": "instagram_publish",
                    "kind": "integration",
                    "purpose": "Generate and publish an Instagram post",
                    "requires_connection": True,
                }
            ],
        },
        job_brief="Generate and publish an Instagram post every day.",
    )

    requirement = spec["requirements"][0]
    assert requirement["key"] == "instagram_publish"
    assert requirement["status"] == "connection_required"
    assert "content_generation" in requirement["primitives"]
    assert "media_generation" in requirement["primitives"]
    assert "messaging" in requirement["primitives"]



def test_compiler_normalizes_general_execution_graph():
    spec = normalize_compiled_spec(
        {
            "role": "General worker",
            "requirements": [
                {
                    "key": "custom_publish",
                    "kind": "custom",
                    "purpose": "Publish a generated result",
                }
            ],
            "execution_graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "draft",
                        "type": "ai",
                        "depends_on": [],
                        "params": {"prompt": "Create the final content"},
                    },
                    {
                        "id": "publish",
                        "type": "action",
                        "depends_on": ["draft"],
                        "params": {
                            "action_type": "custom_publish",
                            "arguments": {
                                "body": "$nodes.draft.ai_response"
                            },
                        },
                    },
                ],
            },
        },
        job_brief="Create content and publish the result.",
    )

    graph = spec["execution_graph"]
    assert graph["version"] == 1
    assert [node["id"] for node in graph["nodes"]] == ["draft", "publish"]
    assert graph["nodes"][1]["depends_on"] == ["draft"]
    assert (
        graph["nodes"][1]["params"]["arguments"]["body"]
        == "$nodes.draft.ai_response"
    )



def test_web_research_is_native_browser_ability_without_fake_action_setup():
    payload = {
        "role": "Research agent",
        "scope": "business",
        "summary": "Research public websites.",
        "requirements": [
            {
                "key": "web_research",
                "kind": "tool",
                "purpose": "Research public websites",
            }
        ],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "research",
                    "type": "browser",
                    "depends_on": [],
                    "params": {
                        "url": "https://example.com",
                        "actions": [
                            {"op": "extract_text", "selector": "body"}
                        ],
                    },
                }
            ],
        },
    }

    spec = normalize_compiled_spec(
        payload,
        job_brief="Research public websites.",
    )
    requirement = spec["requirements"][0]

    assert requirement["key"] == "web_research"
    assert requirement["status"] == "available"
    assert requirement["delivery_mode"] == "native"
    assert requirement["primitives"] == ["browser_web"]
    assert requirement.get("execution_plan") == []
    assert "web_research" in spec["ready_requirements"]
    assert "web_research" not in spec["build_required"]



def test_compiler_cannot_self_grant_automatic_execution():
    spec = normalize_compiled_spec(
        {
            "role": "Lead worker",
            "scope": "business",
            "summary": "Capture leads.",
            "requirements": [
                {
                    "key": "lead_management",
                    "kind": "module",
                    "purpose": "Capture and follow up sales leads",
                }
            ],
            "permissions": [
                {
                    "action": "lead_management",
                    "mode": "automatic",
                }
            ],
        },
        job_brief="Capture and follow up sales leads.",
    )

    permission = spec["permissions"][0]
    assert permission["action"] == "lead_management"
    assert permission["mode"] == "ask_before"
    assert permission["suggested_mode"] == "automatic"
    assert permission["source"] == "compiler_suggestion"


def test_owner_grant_can_promote_compiler_suggestion_to_automatic():
    from backend.app.api.customer_employee_builder import (
        _effective_compiled_permissions,
    )

    compiled = normalize_compiled_spec(
        {
            "role": "Lead worker",
            "scope": "business",
            "summary": "Capture leads.",
            "requirements": [
                {
                    "key": "lead_management",
                    "kind": "module",
                    "purpose": "Capture and follow up sales leads",
                }
            ],
            "permissions": [
                {
                    "action": "lead_management",
                    "mode": "automatic",
                }
            ],
        },
        job_brief="Capture and follow up sales leads.",
    )

    effective = _effective_compiled_permissions(
        compiled,
        {"owner_permissions": {"lead_management": "automatic"}},
    )
    permission = effective["permissions"][0]
    assert permission["action"] == "lead_management"
    assert permission["mode"] == "automatic"
    assert permission["suggested_mode"] == "automatic"
    assert permission["source"] == "owner"

    action = build_internal_record_action_config(
        requirement={
            "key": "lead_management",
            "purpose": "Capture and follow up sales leads",
            "fulfillment_mode": "xvond_internal",
        },
        spec=effective,
    )
    assert action["enabled"] is True
    assert action["confirmation_required"] is False


def test_owner_never_disables_generated_action_contract():
    from backend.app.api.customer_employee_builder import (
        _effective_compiled_permissions,
    )

    compiled = normalize_compiled_spec(
        {
            "role": "Lead worker",
            "scope": "business",
            "summary": "Capture leads.",
            "requirements": [
                {
                    "key": "lead_management",
                    "kind": "module",
                    "purpose": "Capture and follow up sales leads",
                }
            ],
            "permissions": [
                {
                    "action": "lead_management",
                    "mode": "ask_before",
                }
            ],
        },
        job_brief="Capture and follow up sales leads.",
    )
    effective = _effective_compiled_permissions(
        compiled,
        {"owner_permissions": {"lead_management": "never"}},
    )
    action = build_internal_record_action_config(
        requirement={
            "key": "lead_management",
            "purpose": "Capture and follow up sales leads",
            "fulfillment_mode": "xvond_internal",
        },
        spec=effective,
    )
    assert action["enabled"] is False
    assert action["confirmation_required"] is True
