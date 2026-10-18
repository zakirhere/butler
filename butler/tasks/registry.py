from collections.abc import Awaitable, Callable

TaskFn = Callable[[dict], Awaitable[dict]]
ApprovalPolicy = str

_registry: dict[str, TaskFn] = {}
_policies: dict[str, ApprovalPolicy] = {}


def task(name: str, *, approval: ApprovalPolicy = "automatic"):
    """Decorator that registers an async function as a runnable task by name."""

    def decorator(fn: TaskFn) -> TaskFn:
        _registry[name] = fn
        if approval not in {"automatic", "approval_required"}:
            raise ValueError("approval must be automatic or approval_required")
        _policies[name] = approval
        return fn

    return decorator


def get_task(name: str) -> TaskFn | None:
    return _registry.get(name)


def list_tasks() -> list[str]:
    return sorted(_registry)


def get_policy(name: str) -> ApprovalPolicy:
    return _policies.get(name, "automatic")
