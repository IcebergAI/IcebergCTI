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
# - help_content dataclass fields (href/tagline/.../points): read only in
#   templates/help.html, which Vulture can't see.

_.dispatch  # unused method (src/iceberg/auth/csrf.py:40)
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
href  # unused variable (src/iceberg/help_content.py:25)
tagline  # unused variable (src/iceberg/help_content.py:33)
workflow  # unused variable (src/iceberg/help_content.py:34)
can  # unused variable (src/iceberg/help_content.py:35)
cannot  # unused variable (src/iceberg/help_content.py:36)
key_screens  # unused variable (src/iceberg/help_content.py:37)
concepts  # unused variable (src/iceberg/help_content.py:38)
term  # unused variable (src/iceberg/help_content.py:46)
points  # unused variable (src/iceberg/help_content.py:49)
