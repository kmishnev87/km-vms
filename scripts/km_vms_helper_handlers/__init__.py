"""No dispatch entry point: handlers require the facade\u0027s explicit capabilities."""
from .update import UpdateOperationHandler
from .restore import RestoreOperationHandler
from .rollback import RestoreRollbackHandler
from .restore_executor import RestoreCommandExecutor
from .publisher import PublishedSourceError, verify_published_source
from .diagnostics import service_log_request
