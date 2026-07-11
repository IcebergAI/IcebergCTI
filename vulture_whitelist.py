# Vulture whitelist — symbols Vulture can't see used, but which are.
#
# Regenerate after intentional changes with:
#   vulture src/iceberg --ignore-decorators "@router.*,@app.*,@event.*,@asynccontextmanager,@model_validator,@property" --exclude "*/migrations/*" --make-whitelist > vulture_whitelist.py
# then re-add this header. Review the diff and DELETE any line that has genuinely
# become dead code, so Vulture keeps catching real cases.
#
# Why each is a false positive:
# - dispatch: Starlette BaseHTTPMiddleware interface method (called by the framework).
# - model_config: pydantic-settings configuration attribute.
# - enum members: controlled-vocabulary values stored in the DB / chosen in forms,
#   not always referenced by Python name.
# - model columns / relationships: SQLModel fields populated/read by the ORM,
#   templates, JSON serialisation or migration-compatible persistence.
# - script entry points: referenced by pyproject console_scripts.
# - HTMLParser callbacks: invoked by the stdlib parser.
# - help_content dataclass fields: read only in templates/help.html.

_.dispatch  # unused method (src/iceberg/auth/csrf.py:40)
_.background  # Starlette response.background (src/iceberg/auth/audit_middleware.py)
DISSEMINATION  # unused variable (AuditCategory enum, src/iceberg/models.py)
model_config  # unused variable (src/iceberg/config.py:14)
TACTICAL  # unused variable (src/iceberg/models.py:31)
FULL  # unused variable (src/iceberg/models.py:87)
EXEC_BRIEF  # unused variable (src/iceberg/models.py:88)
ONE_PAGER  # unused variable (src/iceberg/models.py:89)
SATISFIED  # unused variable (src/iceberg/models.py:102)
CLOSED  # unused variable (src/iceberg/models.py:103)
NOT_USEFUL  # unused variable (ProductUsefulness enum, src/iceberg/models.py)
PARTIALLY_MET  # unused variable (RfiSatisfaction enum, src/iceberg/models.py)
NOT_MET  # unused variable (RfiSatisfaction enum, src/iceberg/models.py)
ACTOR  # unused variable (src/iceberg/models.py:111)
CAMPAIGN  # unused variable (src/iceberg/models.py:112)
MALWARE  # unused variable (src/iceberg/models.py:113)
TECHNIQUE  # unused variable (src/iceberg/models.py:114)
SECTOR  # unused variable (src/iceberg/models.py:115)
TOPIC  # unused variable (src/iceberg/models.py:116)
ESPIONAGE  # unused variable (src/iceberg/models.py:124)
FINANCIAL  # unused variable (src/iceberg/models.py:125)
HACKTIVISM  # unused variable (src/iceberg/models.py:126)
DESTRUCTIVE  # unused variable (src/iceberg/models.py:127)
INFLUENCE  # unused variable (src/iceberg/models.py:128)
USES  # unused variable (src/iceberg/models.py:136)
ATTRIBUTED_TO  # unused variable (src/iceberg/models.py:137)
VARIANT_OF  # unused variable (src/iceberg/models.py:138)
TARGETS  # unused variable (src/iceberg/models.py:139)
RELATED_TO  # unused variable (src/iceberg/models.py:140)
captured_at  # unused variable (src/iceberg/models.py:243)
grading_origin  # unused variable (src/iceberg/models.py:308)
grading_engine  # unused variable (src/iceberg/models.py:309)
grading_error  # unused variable (src/iceberg/models.py:311)
graded_at  # unused variable (src/iceberg/models.py:312)
file_size  # unused variable (src/iceberg/models.py:334)
uploaded_at  # unused variable (src/iceberg/models.py:336)
reviewer_id  # unused variable (src/iceberg/models.py:361)
group_id  # SQLModel link-table FK (src/iceberg/models.py:409)
group_id  # SQLModel link-table FK (src/iceberg/models.py:420)
_.members  # SQLModel relationship / response serialization (src/iceberg/api/audience.py:35)
members  # SQLModel relationship (src/iceberg/models.py:466)
subscribers  # SQLModel relationship (src/iceberg/models.py:836)
ai_embeddings_enabled  # configuration field reserved for vector backend selection
ai_embedding_model  # configuration field reserved for vector backend selection
_.migrate_main  # console script entry point (pyproject.toml)
_.prune_renders_main  # console script entry point (pyproject.toml)
_.rebuild_related_main  # console script entry point (pyproject.toml)
_.worker_main  # console script entry point (pyproject.toml)
_.verify_files_main  # console script entry point (pyproject.toml)
_.reviewer_id  # unused attribute (src/iceberg/services/lifecycle.py:50)
_.reviewer_id  # unused attribute (src/iceberg/services/lifecycle.py:55)
_.handle_starttag  # unused method (src/iceberg/services/source_grading.py:181)
attrs  # unused variable (src/iceberg/services/source_grading.py:181)
_.handle_endtag  # unused method (src/iceberg/services/source_grading.py:187)
_.handle_data  # unused method (src/iceberg/services/source_grading.py:193)
_.last_fetched_at  # unused attribute (src/iceberg/services/feeds.py)
_.last_status  # unused attribute (src/iceberg/services/feeds.py)
_.fetch_error  # unused attribute (src/iceberg/services/feeds.py)
_.grading_origin  # unused attribute (src/iceberg/services/source_grading.py:520)
_.grading_engine  # unused attribute (src/iceberg/services/source_grading.py:521)
_.grading_error  # unused attribute (src/iceberg/services/source_grading.py:523)
_.graded_at  # unused attribute (src/iceberg/services/source_grading.py:524)
href  # unused variable (src/iceberg/help_content.py:25)
tagline  # unused variable (src/iceberg/help_content.py:33)
workflow  # unused variable (src/iceberg/help_content.py:34)
can  # unused variable (src/iceberg/help_content.py:35)
cannot  # unused variable (src/iceberg/help_content.py:36)
key_screens  # unused variable (src/iceberg/help_content.py:37)
concepts  # unused variable (src/iceberg/help_content.py:38)
term  # unused variable (src/iceberg/help_content.py:46)
points  # unused variable (src/iceberg/help_content.py:49)

# ReportMispEvent fields — read only in templates (report_view.html) + tests.
event_id  # unused variable (src/iceberg/models.py)
pushed_at  # unused variable (src/iceberg/models.py)
_.event_id  # unused attribute (src/iceberg/services/misp.py)
_.pushed_at  # unused attribute (src/iceberg/services/misp.py)
# Future LLM/AI phase seam — IOC auto-extraction (not wired yet).
extract  # unused function (src/iceberg/services/iocs.py)
# Conventional classmethod first-arg on the @field_validator (config.py:_validate_ai_backend).
cls  # unused variable (pydantic @field_validator first arg, src/iceberg/config.py)
# Test-only cache reset (tests/conftest.py clears the process-global LRU per test).
clear_bundle_cache  # unused function (src/iceberg/services/taxii.py)

# Durable jobs, taxonomy merge lineage and import results are ORM/API fields.
merged_at  # unused variable (src/iceberg/models.py)
leased_at  # unused variable (src/iceberg/models.py)
leased_by  # unused variable (src/iceberg/models.py)
started_at  # unused variable (src/iceberg/models.py)
completed_at  # unused variable (src/iceberg/models.py)
skipped  # unused variable (src/iceberg/services/attack_import.py)
_.leased_by  # unused attribute (src/iceberg/services/jobs.py)
_.completed_at  # unused attribute (src/iceberg/services/jobs.py)
_.merged_at  # unused attribute (src/iceberg/services/tags.py)

# Compatibility/service entry points invoked dynamically or by downstream callers.
send_notifications  # unused function (src/iceberg/services/dissemination.py)
send_webhook_notification  # unused function (src/iceberg/services/dissemination.py)
schedule_notifications  # unused function (src/iceberg/services/dissemination.py)
queue_dissemination  # unused function (src/iceberg/services/dissemination.py)
fetch_all_enabled_once  # unused function (src/iceberg/services/feeds.py)
_.connect_unix_socket  # unused attribute (httpcore backend protocol method)
