from enum import Enum

LANG = "Python"
ACK_TIMEOUT = 1000
RESPONSE_TIMEOUT = 1000
STREAM_LEN = 1024
MAX_BLOCK = 999999999999999999
DEFAULT_REDIS_PORT = 6379
DEFAULT_METRICS_PORT = 6380
HEALTHCHECK_RETRY_INTERVAL = 5
REDIS_PIPELINE_POOL_SIZE = 20
DEFAULT_REDIS_SOCKET = "/shared/redis.sock"
DEFAULT_METRICS_SOCKET = "/shared/metrics.sock"

# Error codes
ATOM_NO_ERROR = 0
ATOM_INTERNAL_ERROR = 1
ATOM_REDIS_ERROR = 2
ATOM_COMMAND_NO_ACK = 3
ATOM_COMMAND_NO_RESPONSE = 4
ATOM_COMMAND_INVALID_DATA = 5
ATOM_COMMAND_UNSUPPORTED = 6
ATOM_CALLBACK_FAILED = 7
ATOM_LANGUAGE_ERRORS_BEGIN = 100
ATOM_USER_ERRORS_BEGIN = 1000

# Reserved Commands
HEALTHCHECK_COMMAND = "healthcheck"
VERSION_COMMAND = "version"
COMMAND_LIST_COMMAND = "command_list"
RESERVED_COMMANDS = [
    COMMAND_LIST_COMMAND,
    VERSION_COMMAND,
    HEALTHCHECK_COMMAND
]

# Metrics
METRICS_ELEMENT_LABEL = "element"
METRICS_TYPE_LABEL = "type"
METRICS_HOST_LABEL = "container"
METRICS_ATOM_VERSION_LABEL = "version"
METRICS_SUBTYPE_LABEL = "subtype"
METRICS_DEVICE_LABEL = "device"
METRICS_LANGUAGE_LABEL = "language"
METRICS_LEVEL_LABEL = "level"
METRICS_AGGREGATION_LABEL = "agg"
METRICS_AGGREGATION_TYPE_LABEL = "agg_type"
# Metrics default retention -- 1 hour of raw data
METRICS_DEFAULT_RETENTION = 3600000
# Metrics default aggregation rules
METRICS_DEFAULT_AGG_TIMING = [
    # Keep data in 10m buckets for 3 days
    (600000,  259200000),
    # Then keep data in 1h buckets for 30 days
    (3600000, 2592000000)
]

# Metrics logging levels
class MetricsLevel(Enum):
    EMERG = 0
    ALERT = 1
    CRIT = 2
    ERR = 3
    WARNING = 4
    NOTICE = 5
    INFO = 6
    TIMING = 7
    DEBUG = 8
