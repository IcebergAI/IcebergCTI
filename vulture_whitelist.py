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
# - model columns (captured_at/file_size/uploaded_at/reviewer_id/grading_*): SQLModel fields
#   populated and read via the ORM / templates.
# - HTMLParser callbacks: invoked by the stdlib parser.

_.dispatch  # unused method (src/iceberg/auth/csrf.py:40)
model_config  # unused variable (src/iceberg/config.py:14)
TACTICAL  # unused variable (src/iceberg/models.py:31)
FULL  # unused variable (src/iceberg/models.py:87)
EXEC_BRIEF  # unused variable (src/iceberg/models.py:88)
ONE_PAGER  # unused variable (src/iceberg/models.py:89)
SATISFIED  # unused variable (src/iceberg/models.py:102)
CLOSED  # unused variable (src/iceberg/models.py:103)
ACTOR  # unused variable (src/iceberg/models.py:111)
CAMPAIGN  # unused variable (src/iceberg/models.py:112)
MALWARE  # unused variable (src/iceberg/models.py:113)
TECHNIQUE  # unused variable (src/iceberg/models.py:114)
SECTOR  # unused variable (src/iceberg/models.py:115)
TOPIC  # unused variable (src/iceberg/models.py:116)
captured_at  # unused variable (src/iceberg/models.py:243)
grading_origin  # unused variable (src/iceberg/models.py:308)
grading_engine  # unused variable (src/iceberg/models.py:309)
grading_error  # unused variable (src/iceberg/models.py:311)
graded_at  # unused variable (src/iceberg/models.py:312)
file_size  # unused variable (src/iceberg/models.py:334)
uploaded_at  # unused variable (src/iceberg/models.py:336)
reviewer_id  # unused variable (src/iceberg/models.py:361)
_.reviewer_id  # unused attribute (src/iceberg/services/lifecycle.py:50)
_.reviewer_id  # unused attribute (src/iceberg/services/lifecycle.py:55)
_.handle_starttag  # unused method (src/iceberg/services/source_grading.py:181)
attrs  # unused variable (src/iceberg/services/source_grading.py:181)
_.handle_endtag  # unused method (src/iceberg/services/source_grading.py:187)
_.handle_data  # unused method (src/iceberg/services/source_grading.py:193)
_.grading_origin  # unused attribute (src/iceberg/services/source_grading.py:520)
_.grading_engine  # unused attribute (src/iceberg/services/source_grading.py:521)
_.grading_error  # unused attribute (src/iceberg/services/source_grading.py:523)
_.graded_at  # unused attribute (src/iceberg/services/source_grading.py:524)
