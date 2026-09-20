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
    assert action["destination"]["delivery_mode"] == "legacy_plan"
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


def test_owner_never_blocks_generated_action_without_overwriting_operator_enablement():
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
    assert action["enabled"] is True
    assert action["_xvond_permission_mode"] == "never"
    assert action["confirmation_required"] is True



def test_compiler_normalizes_generic_monthly_schedule():
    job_brief = "اعمل تقرير شهري يوم 1 الساعة 9"
    response = """{
      "role":"Reporting employee",
      "scope":"business",
      "summary":"Prepare a monthly report.",
      "tasks":[{"name":"Report","description":"Prepare report","trigger":"monthly"}],
      "requirements":[{
        "key":"monthly_report",
        "kind":"custom",
        "purpose":"Prepare the monthly report",
        "primitives":["scheduler","workflow_engine"],
        "schedule":{
          "kind":"monthly",
          "day_of_month":1,
          "hour":9,
          "minute":0,
          "source_text":"يوم 1 الساعة 9"
        }
      }],
      "permissions":[{"action":"Prepare the monthly report","mode":"automatic"}],
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    requirement = spec["requirements"][0]

    assert requirement["schedule"] == {
        "kind": "monthly",
        "day_of_month": 1,
        "hour": 9,
        "minute": 0,
        "source_text": "يوم 1 الساعة 9",
    }


def test_compiler_normalizes_generic_one_time_schedule():
    job_brief = "نفذ المهمة مرة واحدة بتاريخ 2026-10-01T09:00:00+04:00"
    response = """{
      "role":"One-time task employee",
      "scope":"personal",
      "summary":"Run a one-time task.",
      "tasks":[{"name":"Task","description":"Run once","trigger":"once"}],
      "requirements":[{
        "key":"one_time_task",
        "kind":"custom",
        "purpose":"Run the requested task",
        "primitives":["scheduler","workflow_engine"],
        "schedule":{
          "kind":"once",
          "at":"2026-10-01T09:00:00+04:00",
          "source_text":"2026-10-01T09:00:00+04:00"
        }
      }],
      "permissions":[{"action":"Run the requested task","mode":"automatic"}],
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    requirement = spec["requirements"][0]

    assert requirement["schedule"] == {
        "kind": "once",
        "at": "2026-10-01T09:00:00+04:00",
        "source_text": "2026-10-01T09:00:00+04:00",
    }



def test_compiler_preserves_generic_durable_wait_node():
    job_brief = "جهز المسودة وبعدها انتظر يومين ثم راجع النتيجة"
    response = """{
      "role":"Long-running worker",
      "scope":"personal",
      "summary":"Prepare work, wait, then continue.",
      "tasks":[{"name":"Long task","description":"Prepare and continue later","trigger":"manual"}],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{"type":"manual"},
        "nodes":[
          {
            "id":"prepare",
            "type":"transform",
            "depends_on":[],
            "params":{"values":{"stage":"prepared"}}
          },
          {
            "id":"pause",
            "type":"wait",
            "depends_on":["prepare"],
            "params":{
              "duration":2,
              "unit":"days",
              "source_text":"انتظر يومين"
            }
          },
          {
            "id":"continue",
            "type":"ai",
            "depends_on":["pause"],
            "params":{"prompt":"Review the result after the requested wait."}
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    nodes = spec["execution_graph"]["nodes"]

    assert spec["version"] == 14
    assert [node["type"] for node in nodes] == ["transform", "wait", "ai"]
    assert nodes[1]["params"]["duration"] == 2
    assert nodes[1]["params"]["unit"] == "days"
    assert nodes[1]["params"]["source_text"] == "انتظر يومين"



def test_compiler_preserves_generic_correlated_event_wait():
    job_brief = "ابدأ المهمة وانتظر حدث external.result.ready لنفس job_id ثم كمل"
    response = """{
      "role":"Event-driven worker",
      "scope":"business",
      "summary":"Start work and continue when the correlated result arrives.",
      "tasks":[{"name":"Process","description":"Wait for result","trigger":"manual"}],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{"type":"manual"},
        "nodes":[
          {
            "id":"start",
            "type":"transform",
            "depends_on":[],
            "params":{"values":{"job_id":"$input.job_id"}}
          },
          {
            "id":"wait_result",
            "type":"await_event",
            "depends_on":["start"],
            "params":{
              "event":"external.result.ready",
              "match":{"job_id":"$nodes.start.job_id"}
            }
          },
          {
            "id":"continue",
            "type":"ai",
            "depends_on":["wait_result"],
            "params":{
              "prompt":"Continue from the received result.",
              "context":"$nodes.wait_result.payload"
            }
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    nodes = spec["execution_graph"]["nodes"]

    assert spec["version"] == 14
    assert [node["type"] for node in nodes] == [
        "transform",
        "await_event",
        "ai",
    ]
    assert nodes[1]["params"]["event"] == "external.result.ready"
    assert nodes[1]["params"]["match"]["job_id"] == "$nodes.start.job_id"



def test_compiler_drops_unrequested_time_wait():
    job_brief = "جهز المسودة ثم راجع النتيجة"
    response = """{
      "role":"Worker",
      "scope":"personal",
      "summary":"Prepare and review.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{"type":"manual"},
        "nodes":[
          {"id":"prepare","type":"transform","depends_on":[],"params":{"values":{"stage":"prepared"}}},
          {"id":"pause","type":"wait","depends_on":["prepare"],"params":{"duration":2,"unit":"days","source_text":"انتظر يومين"}},
          {"id":"continue","type":"ai","depends_on":["pause"],"params":{"prompt":"Review the result."}}
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    nodes = spec["execution_graph"]["nodes"]

    assert [node["type"] for node in nodes] == ["transform", "ai"]
    assert nodes[1]["depends_on"] == []


def test_compiler_drops_unrequested_event_wait():
    job_brief = "أنشئ الطلب ثم أكمل معالجة البيانات"
    response = """{
      "role":"Worker",
      "scope":"business",
      "summary":"Create and process.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{"type":"manual"},
        "nodes":[
          {"id":"create","type":"transform","depends_on":[],"params":{"values":{"status":"created"}}},
          {"id":"pause","type":"await_event","depends_on":["create"],"params":{"event":"payment.confirmed","source_text":"انتظر تأكيد الدفع"}},
          {"id":"continue","type":"ai","depends_on":["pause"],"params":{"prompt":"Continue processing."}}
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    nodes = spec["execution_graph"]["nodes"]

    assert [node["type"] for node in nodes] == ["transform", "ai"]
    assert nodes[1]["depends_on"] == []


def test_compiler_keeps_grounded_waits_inside_foreach():
    job_brief = "لكل عنصر جهزه وانتظر ساعة ثم كمل"
    response = """{
      "role":"Batch worker",
      "scope":"personal",
      "summary":"Process items with a requested pause.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{"type":"manual"},
        "nodes":[
          {
            "id":"each",
            "type":"foreach",
            "depends_on":[],
            "params":{
              "items":"$input.items",
              "graph":{
                "version":1,
                "nodes":[
                  {"id":"pause","type":"wait","depends_on":[],"params":{"duration":1,"unit":"hours","source_text":"انتظر ساعة"}}
                ]
              }
            }
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)
    nested = spec["execution_graph"]["nodes"][0]["params"]["graph"]["nodes"]

    assert len(nested) == 1
    assert nested[0]["type"] == "wait"
    assert nested[0]["params"]["source_text"] == "انتظر ساعة"



def test_compiler_normalizes_multiple_independent_execution_routines():
    job_brief = (
        "كل يوم الساعة 8 اعمل ملخص صباحي. "
        "وعندما يصل حدث lead.created حلل الليد بشكل مستقل."
    )
    response = """{
      "role":"Operations employee",
      "scope":"business",
      "summary":"Run independent morning and lead routines.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_routines":[
        {
          "id":"morning_summary",
          "name":"Morning summary",
          "graph":{
            "version":1,
            "trigger":{
              "type":"schedule",
              "schedule":{
                "kind":"daily",
                "hour":8,
                "minute":0,
                "timezone":"Asia/Muscat",
                "source_text":"كل يوم الساعة 8"
              }
            },
            "nodes":[
              {
                "id":"summarize",
                "type":"ai",
                "depends_on":[],
                "params":{"prompt":"Prepare the morning summary."}
              }
            ]
          }
        },
        {
          "id":"lead_review",
          "name":"Lead review",
          "graph":{
            "version":1,
            "trigger":{"type":"event","event":"lead.created"},
            "nodes":[
              {
                "id":"review",
                "type":"ai",
                "depends_on":[],
                "params":{"prompt":"Analyze the new lead.","context":"$input"}
              }
            ]
          }
        }
      ],
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["version"] == 14
    assert [item["id"] for item in spec["execution_routines"]] == [
        "morning_summary",
        "lead_review",
    ]
    assert spec["execution_routines"][0]["graph"]["trigger"]["type"] == "schedule"
    assert spec["execution_routines"][1]["graph"]["trigger"] == {
        "type": "event",
        "event": "lead.created",
    }
    assert spec["execution_graph"] == spec["execution_routines"][0]["graph"]


def test_compiler_maps_legacy_execution_graph_to_primary_routine():
    job_brief = "لما شغله يدويًا لخص البيانات"
    response = """{
      "role":"Manual analyst",
      "scope":"personal",
      "summary":"Summarize supplied data.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{"type":"manual"},
        "nodes":[
          {
            "id":"summarize",
            "type":"ai",
            "depends_on":[],
            "params":{"prompt":"Summarize the supplied data."}
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["version"] == 14
    assert len(spec["execution_routines"]) == 1
    assert spec["execution_routines"][0]["id"] == "primary"
    assert spec["execution_routines"][0]["graph"] == spec["execution_graph"]
    assert spec["execution_graph"]["nodes"][0]["id"] == "summarize"


def test_compiler_grounds_waits_inside_each_independent_routine():
    job_brief = (
        "روتين أول انتظر ساعة ثم كمل. "
        "روتين ثاني انتظر حدث external.ready ثم كمل."
    )
    response = """{
      "role":"Durable worker",
      "scope":"personal",
      "summary":"Run two durable routines.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_routines":[
        {
          "id":"time_wait",
          "name":"Time wait",
          "graph":{
            "version":1,
            "trigger":{"type":"manual"},
            "nodes":[
              {
                "id":"pause",
                "type":"wait",
                "depends_on":[],
                "params":{"duration":1,"unit":"hours","source_text":"انتظر ساعة"}
              }
            ]
          }
        },
        {
          "id":"event_wait",
          "name":"Event wait",
          "graph":{
            "version":1,
            "trigger":{"type":"manual"},
            "nodes":[
              {
                "id":"pause",
                "type":"await_event",
                "depends_on":[],
                "params":{
                  "event":"external.ready",
                  "source_text":"انتظر حدث external.ready"
                }
              }
            ]
          }
        }
      ],
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["execution_routines"][0]["graph"]["nodes"][0]["type"] == "wait"
    assert spec["execution_routines"][1]["graph"]["nodes"][0]["type"] == "await_event"



def test_compiler_fails_ungrounded_schedule_trigger_closed():
    job_brief = "لخص البيانات عندما أشغلك يدويًا"
    response = """{
      "role":"Analyst",
      "scope":"personal",
      "summary":"Summarize supplied data.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{
          "type":"schedule",
          "schedule":{
            "kind":"daily",
            "hour":8,
            "minute":0,
            "timezone":"Asia/Muscat",
            "source_text":"كل يوم الساعة 8"
          }
        },
        "nodes":[
          {
            "id":"summarize",
            "type":"ai",
            "depends_on":[],
            "params":{"prompt":"Summarize the supplied data."}
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["execution_graph"]["trigger"] == {"type": "schedule"}
    assert spec["execution_routines"][0]["graph"]["trigger"] == {"type": "schedule"}



def test_compiler_inherits_graph_schedule_only_from_grounded_requirement_schedule():
    job_brief = "Every 60 minutes generate a summary and save it."
    response = """{
      "role":"Scheduled worker",
      "scope":"personal",
      "summary":"Generate and save a recurring summary.",
      "tasks":[],
      "requirements":[
        {
          "key":"save_summary",
          "kind":"custom",
          "purpose":"Save the generated summary",
          "primitives":["scheduler","workflow_engine"],
          "schedule":{
            "kind":"interval",
            "every_minutes":60,
            "source_text":"Every 60 minutes"
          }
        }
      ],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{
          "type":"schedule",
          "schedule":{"kind":"interval","every_minutes":60}
        },
        "nodes":[
          {
            "id":"summarize",
            "type":"ai",
            "depends_on":[],
            "params":{"prompt":"Generate the summary."}
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["execution_graph"]["trigger"]["schedule"] == {
        "kind": "interval",
        "every_minutes": 60,
        "source_text": "Every 60 minutes",
    }


def test_compiler_fails_invented_internal_event_trigger_closed():
    job_brief = "حلل البيانات فقط عندما أشغلك بنفسي"
    response = """{
      "role":"Manual analyst",
      "scope":"personal",
      "summary":"Analyze supplied data.",
      "tasks":[],
      "requirements":[],
      "permissions":[],
      "execution_graph":{
        "version":1,
        "trigger":{
          "type":"event",
          "event":"lead.created",
          "source_text":"عندما يصل lead جديد"
        },
        "nodes":[
          {
            "id":"analyze",
            "type":"ai",
            "depends_on":[],
            "params":{"prompt":"Analyze the supplied data."}
          }
        ]
      },
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["execution_graph"]["trigger"] == {"type": "event"}
    assert spec["execution_routines"][0]["graph"]["trigger"] == {"type": "event"}



def test_compiler_scopes_each_routine_to_only_its_requirements():
    job_brief = (
        "Every morning monitor source A. "
        "Every evening monitor source B."
    )
    response = """{
      "role":"Monitoring employee",
      "scope":"personal",
      "summary":"Run two independent monitors.",
      "tasks":[],
      "requirements":[
        {
          "key":"source_a",
          "kind":"custom",
          "purpose":"Monitor source A",
          "runtime_inputs":{"target":"A"},
          "primitives":["scheduler","workflow_engine"],
          "schedule":{
            "kind":"daily",
            "hour":8,
            "minute":0,
            "timezone":"UTC",
            "source_text":"Every morning"
          }
        },
        {
          "key":"source_b",
          "kind":"custom",
          "purpose":"Monitor source B",
          "runtime_inputs":{"target":"B"},
          "primitives":["scheduler","workflow_engine"],
          "schedule":{
            "kind":"daily",
            "hour":18,
            "minute":0,
            "timezone":"UTC",
            "source_text":"Every evening"
          }
        }
      ],
      "permissions":[],
      "execution_routines":[
        {
          "id":"morning",
          "name":"Morning monitor",
          "requirement_keys":["source_a","source_b","invented"],
          "graph":{
            "version":1,
            "trigger":{
              "type":"schedule",
              "schedule":{
                "kind":"daily",
                "hour":8,
                "minute":0,
                "timezone":"UTC",
                "source_text":"Every morning"
              }
            },
            "nodes":[
              {
                "id":"finish",
                "type":"notify",
                "depends_on":[],
                "params":{"message":"Morning complete"}
              }
            ]
          }
        },
        {
          "id":"evening",
          "name":"Evening monitor",
          "requirement_keys":["source_b"],
          "graph":{
            "version":1,
            "trigger":{
              "type":"schedule",
              "schedule":{
                "kind":"daily",
                "hour":18,
                "minute":0,
                "timezone":"UTC",
                "source_text":"Every evening"
              }
            },
            "nodes":[
              {
                "id":"finish",
                "type":"notify",
                "depends_on":[],
                "params":{"message":"Evening complete"}
              }
            ]
          }
        }
      ],
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["version"] == 14
    assert spec["execution_routines"][0]["requirement_keys"] == [
        "source_a",
        "source_b",
    ]
    assert spec["execution_routines"][1]["requirement_keys"] == ["source_b"]


def test_compiler_adds_action_requirement_to_routine_scope():
    job_brief = "When I run it manually, save the result."
    response = """{
      "role":"Saver",
      "scope":"personal",
      "summary":"Save a result.",
      "tasks":[],
      "requirements":[
        {
          "key":"save_result",
          "kind":"custom",
          "purpose":"Save the result",
          "primitives":["workflow_engine"]
        }
      ],
      "permissions":[],
      "execution_routines":[
        {
          "id":"save",
          "name":"Save result",
          "requirement_keys":[],
          "graph":{
            "version":1,
            "trigger":{"type":"manual"},
            "nodes":[
              {
                "id":"save",
                "type":"action",
                "depends_on":[],
                "params":{"action_type":"save_result","arguments":{}}
              }
            ]
          }
        }
      ],
      "setup_questions":[]
    }"""

    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["execution_routines"][0]["requirement_keys"] == ["save_result"]


def test_compiler_preserves_safe_dynamic_api_operations_and_drops_secrets():
    payload = {
        "role": "Novel API worker",
        "scope": "business",
        "summary": "Use a customer API.",
        "requirements": [{
            "key": "never_seen_vendor_action",
            "kind": "integration",
            "purpose": "Create a vendor object",
            "requires_connection": True,
            "fulfillment_mode": "external_connection",
            "integration_operations": {
                "create_object": {
                    "method": "POST",
                    "endpoint": "/v7/objects",
                    "timeout": 12,
                    "headers": {"Authorization": "Bearer must-not-survive"},
                },
                "bad_absolute": {"method": "POST", "endpoint": "https://evil.example/x"},
                "bad_method": {"method": "TRACE", "endpoint": "/trace"},
            },
        }],
    }
    spec = normalize_compiled_spec(payload, job_brief="Connect my vendor API and create objects.")
    requirement = spec["requirements"][0]
    assert requirement["status"] == "connection_required"
    assert requirement["integration_operations"] == {
        "create_object": {
            "method": "POST",
            "endpoint": "/v7/objects",
            "input_mode": "json",
            "timeout": 12.0,
        }
    }


def test_compiler_message_exposes_bounded_connection_capabilities_without_runtime_ids():
    message = build_compiler_user_message(
        job_brief="Create orders in my connected vendor system.",
        requested_channels=[],
        available_connections=[{
            "name": "Vendor API",
            "type": "custom_api",
            "capabilities": [],
            "operations": {
                "create_order": {
                    "method": "POST",
                    "endpoint": "/orders",
                    "input_mode": "json",
                    "description": "Create an order",
                }
            },
        }],
    )
    assert "AVAILABLE VALIDATED CONNECTED SYSTEMS" in message
    assert "create_order" in message
    assert "/orders" in message
    assert "integration_id" not in message
    assert "api_key" not in message


def test_novel_external_capability_preserves_bounded_discovery_plan():
    brief = "Connect to Acme Fleet API and dispatch vehicles. Docs: https://docs.acme.example/openapi.json"
    spec = normalize_compiled_spec(
        {
            "role": "Fleet dispatcher",
            "requirements": [{
                "key": "fleet_dispatch",
                "kind": "integration",
                "purpose": "Dispatch vehicles in Acme Fleet",
                "requires_connection": True,
                "fulfillment_mode": "external_connection",
                "primitives": ["http_api", "workflow_engine"],
                "discovery": {
                    "needed": True,
                    "capability": "Dispatch vehicles through Acme Fleet API",
                    "service_hint": "Acme Fleet",
                    "docs_url": "https://docs.acme.example/openapi.json",
                    "search_queries": [
                        "Acme Fleet API OpenAPI",
                        "Acme Fleet dispatch API docs",
                    ],
                    "customer_access": "api_key",
                },
            }],
        },
        job_brief=brief,
    )
    discovery = spec["requirements"][0]["discovery"]
    assert discovery["status"] == "pending_discovery"
    assert discovery["service_hint"] == "Acme Fleet"
    assert discovery["docs_url"] == "https://docs.acme.example/openapi.json"
    assert discovery["customer_access"] == "api_key"


def test_discovery_drops_hallucinated_docs_url_and_bounds_queries():
    spec = normalize_compiled_spec(
        {
            "role": "Novel worker",
            "requirements": [{
                "key": "novel_external",
                "kind": "integration",
                "requires_connection": True,
                "discovery": {
                    "needed": True,
                    "capability": "Use a novel vendor API",
                    "docs_url": "https://invented.example/openapi.json",
                    "search_queries": [f"query {i}" for i in range(10)],
                    "customer_access": "something_invalid",
                },
            }],
        },
        job_brief="Use a novel vendor API.",
    )
    discovery = spec["requirements"][0]["discovery"]
    assert discovery["docs_url"] == ""
    assert len(discovery["search_queries"]) == 5
    assert discovery["customer_access"] == "unknown"

def test_compiler_preserves_connected_system_request_contract_metadata():
    spec = normalize_compiled_spec(
        {
            "role": "Record employee",
            "requirements": [
                {
                    "key": "specialist_records",
                    "kind": "integration",
                    "purpose": "Create records",
                    "requires_connection": True,
                    "fulfillment_mode": "external_connection",
                    "integration_operations": {
                        "execute": {
                            "method": "POST",
                            "endpoint": "/records/{account_id}",
                            "input_mode": "json",
                            "path_params": ["account_id"],
                            "required_query_params": ["locale"],
                            "required_json_fields": ["customer_name"],
                            "json_fields": [
                                {
                                    "key": "customer_name",
                                    "required": True,
                                    "type": "string",
                                    "description": "Customer name",
                                }
                            ],
                        }
                    },
                }
            ],
        },
        job_brief="Create records in my connected specialist system.",
    )

    operation = spec["requirements"][0]["integration_operations"]["execute"]
    assert operation["path_params"] == ["account_id"]
    assert operation["required_query_params"] == ["locale"]
    assert operation["required_json_fields"] == ["customer_name"]
    assert operation["json_fields"][0]["key"] == "customer_name"

def test_compiler_preserves_declared_connected_api_query_parameters():
    spec = normalize_compiled_spec(
        {
            "role": "Search employee",
            "requirements": [
                {
                    "key": "vendor_search",
                    "kind": "integration",
                    "purpose": "Search connected vendor records",
                    "requires_connection": True,
                    "fulfillment_mode": "external_connection",
                    "integration_operations": {
                        "lookup": {
                            "method": "GET",
                            "endpoint": "/records",
                            "input_mode": "query",
                            "query_params": ["status", "limit"],
                            "required_query_params": ["status"],
                        }
                    },
                }
            ],
        },
        job_brief="Search records in my connected vendor system.",
    )

    operation = spec["requirements"][0]["integration_operations"]["lookup"]
    assert operation["query_params"] == ["status", "limit"]
    assert operation["required_query_params"] == ["status"]

def test_compiler_preserves_connected_api_response_contract_metadata():
    spec = normalize_compiled_spec(
        {
            "role": "Order employee",
            "requirements": [
                {
                    "key": "vendor_orders",
                    "kind": "integration",
                    "purpose": "Create vendor orders",
                    "requires_connection": True,
                    "fulfillment_mode": "external_connection",
                    "integration_operations": {
                        "create_order": {
                            "method": "POST",
                            "endpoint": "/orders",
                            "input_mode": "json",
                            "response_status": "201",
                            "response_kind": "object",
                            "response_fields": [
                                {
                                    "key": "id",
                                    "required": True,
                                    "type": "string",
                                    "description": "Created order identifier",
                                },
                                {
                                    "key": "state",
                                    "required": False,
                                    "type": "string",
                                },
                            ],
                        }
                    },
                }
            ],
        },
        job_brief="Create orders in my connected vendor system.",
    )

    operation = spec["requirements"][0]["integration_operations"]["create_order"]
    assert operation["response_status"] == "201"
    assert operation["response_kind"] == "object"
    assert [item["key"] for item in operation["response_fields"]] == ["id", "state"]
    assert operation["response_fields"][0]["required"] is True

def test_compiler_preserves_connected_api_form_request_contract():
    spec = normalize_compiled_spec(
        {
            "role": "Session employee",
            "requirements": [
                {
                    "key": "vendor_session",
                    "kind": "integration",
                    "purpose": "Create a vendor session",
                    "requires_connection": True,
                    "fulfillment_mode": "external_connection",
                    "integration_operations": {
                        "create_session": {
                            "method": "POST",
                            "endpoint": "/session",
                            "input_mode": "form",
                            "required_form_fields": ["username"],
                            "form_fields": [
                                {
                                    "key": "username",
                                    "required": True,
                                    "type": "string",
                                },
                                {
                                    "key": "remember",
                                    "required": False,
                                    "type": "boolean",
                                },
                            ],
                        }
                    },
                }
            ],
        },
        job_brief="Create a session in my connected vendor system.",
    )

    operation = spec["requirements"][0]["integration_operations"]["create_session"]
    assert operation["input_mode"] == "form"
    assert operation["required_form_fields"] == ["username"]
    assert [item["key"] for item in operation["form_fields"]] == [
        "username",
        "remember",
    ]
