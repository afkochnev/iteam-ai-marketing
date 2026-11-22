"""Process admission is selected once, before business imports or connections.

This is a cold-start boundary, not an activation authority or a live revocation
mechanism. A passive process must be replaced to become active. Changing mutable
settings or the environment cannot grant a running process new capabilities.
"""

from dataclasses import dataclass
from typing import Literal

from app.core.config import settings

ADMISSION_PROTOCOL = 1
AdmissionMode = Literal["active", "passive"]


class RuntimeAdmissionError(RuntimeError):
    def __init__(
        self,
        code: Literal["RUNTIME_PASSIVE", "RUNTIME_ADMISSION_MODE_INVALID"] = "RUNTIME_PASSIVE",
    ) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Admission:
    mode: AdmissionMode


def _boot_admission() -> _Admission:
    requested = settings.runtime_admission_mode
    if requested not in {"active", "passive"}:
        # Do not interpolate an untrusted environment value into startup logs.
        raise RuntimeAdmissionError("RUNTIME_ADMISSION_MODE_INVALID")
    if requested == "passive" or settings.app_env.lower() == "maintenance":
        return _Admission("passive")
    return _Admission("active")


_ADMISSION = _boot_admission()


def is_passive_runtime() -> bool:
    return _ADMISSION.mode == "passive"


def require_active_runtime() -> None:
    if is_passive_runtime():
        raise RuntimeAdmissionError()


def admission_status(*, business_ready: bool = False) -> dict[str, object]:
    return {
        "admission_protocol": ADMISSION_PROTOCOL,
        "admission_mode": _ADMISSION.mode,
        "business_ready": business_ready and not is_passive_runtime(),
        "build_sha": settings.build_sha,
    }
