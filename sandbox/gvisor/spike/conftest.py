"""Test-process configuration for the gVisor spike tests.

These tests import ``app`` but live outside ``apps/api/tests``, so that
conftest does not apply.  app.config's Settings singleton refuses the public
development JWT secret unless APP_ENV is explicitly development or test; a
random per-run secret keeps APP_ENV unset, as in the API test suite.
"""

import os
import secrets

os.environ.setdefault("JWT_SECRET_KEY", secrets.token_urlsafe(48))
