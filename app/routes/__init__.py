"""
Routes package.

Each module under this package registers handlers with the global router.
Importing this package triggers all sub-module imports.
"""
from app.routes import dashboard  # noqa: F401
from app.routes import profile    # noqa: F401
from app.routes import income     # noqa: F401
from app.routes import bills      # noqa: F401
from app.routes import expenses   # noqa: F401
from app.routes import savings    # noqa: F401
from app.routes import debts      # noqa: F401
from app.routes import notifications  # noqa: F401
