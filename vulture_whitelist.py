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
# - model columns (captured_at/file_size/uploaded_at/reviewer_id): SQLModel fields
#   populated and read via the ORM / templates.

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
file_size  # unused variable (src/iceberg/models.py:301)
uploaded_at  # unused variable (src/iceberg/models.py:303)
reviewer_id  # unused variable (src/iceberg/models.py:328)
_.reviewer_id  # unused attribute (src/iceberg/services/lifecycle.py:50)
_.reviewer_id  # unused attribute (src/iceberg/services/lifecycle.py:55)
