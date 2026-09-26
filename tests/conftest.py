"""pytest 公共夹具与日志配置。"""

import logging

from app.logging_setup import configure_logging

configure_logging(logging.DEBUG)
