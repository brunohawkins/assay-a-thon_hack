"""Settings from the environment, machine detection, and the worker-pool plan.

Every knob is an environment variable named A2C_* (or a flag on `a2c start`). Secrets
(S3 keys, a model API key) come from the environment only, never from a file in this repo.
The URL-signing key is generated on first start into the data directory, mode 0600.

THE POOL PLAN. The worker runs two kinds of slot:

    light   stages s0-s12 apart from the OpenFOAM fan-out: seconds to minutes, ~1 core
    foam    one OpenFOAM case (chip x protocol step), `foam_cores_per_case` cores each

Defaults, sized from the machine at start-up (this Mac: 8 cores, 24 GB):

    light slots           A2C_LIGHT_SLOTS            2
    cores per foam case   A2C_FOAM_CORES_PER_CASE    min(6, cores - light slots)
    foam cases at once    A2C_FOAM_MAX_PARALLEL      1 (the cap), and never more than
                          cores, memory (A2C_FOAM_MEM_GB_PER_CASE, 8) or scratch disk
                          (A2C_FOAM_DISK_GB_PER_CASE, 20) allow

The per-case core count reaches OpenFOAM as FOAM_CORES, which assay2chip/foam/runner.py
already honours; nothing here launches OpenFOAM itself.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError("%s=%r is not an integer" % (name, value))


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError("%s=%r is not a number" % (name, value))


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def default_data_dir() -> Path:
    """Outside the repo on purpose: uploads can be unpublished protocols."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "assay2chip"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "assay2chip"


# --- machine -------------------------------------------------------------------------

@dataclass
class Machine:
    arch: str
    cores: int
    mem_gb: float
    disk_free_gb: float
    os: str

    def to_json(self) -> dict:
        return asdict(self)


def _mem_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024.0 * 1024.0)
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            return int(out) / 1024.0 ** 3
        except Exception:
            pass
    return 4.0


def detect_machine(scratch: Optional[Path] = None) -> Machine:
    probe = Path(scratch) if scratch is not None else Path.cwd()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    disk = shutil.disk_usage(str(probe)).free / 1024.0 ** 3
    return Machine(arch=platform.machine(), cores=os.cpu_count() or 1,
                   mem_gb=round(_mem_gb(), 1), disk_free_gb=round(disk, 1),
                   os=platform.system())


# --- the pool plan -------------------------------------------------------------------

@dataclass
class PoolPlan:
    light_slots: int
    foam_slots: int
    foam_cores_per_case: int
    limited_by: str

    def to_json(self) -> dict:
        return asdict(self)


def plan_pool(machine: Machine, *, light_slots: Optional[int] = None,
              foam_cores: Optional[int] = None, foam_cap: int = 1,
              foam_mem_gb: float = 8.0, foam_disk_gb: float = 20.0,
              light_mem_gb: float = 2.0, reserve_mem_gb: float = 4.0) -> PoolPlan:
    """How many light and OpenFOAM slots to run at once. A pure function of its inputs."""
    cores = max(1, machine.cores)
    light = max(1, light_slots if light_slots is not None else 2)
    per_case = foam_cores if foam_cores is not None else min(6, max(1, cores - light))
    per_case = max(1, per_case)
    free_mem = max(0.0, machine.mem_gb - reserve_mem_gb - light * light_mem_gb)
    limits: Dict[str, int] = {
        "cap": max(0, foam_cap),
        "cores": max(1, (cores - light) // per_case) if cores > light else 1,
        "memory": int(free_mem // foam_mem_gb) if foam_mem_gb > 0 else foam_cap,
        "disk": int(machine.disk_free_gb // foam_disk_gb) if foam_disk_gb > 0 else foam_cap,
    }
    foam = min(limits.values())
    limited_by = min(limits, key=lambda k: (limits[k], ["cap", "cores", "memory",
                                                         "disk"].index(k)))
    if foam == 0 and limits["cap"] > 0:
        # Short of memory or disk: one case at a time, slower, rather than a fan-out
        # that waits for ever.
        foam, limited_by = 1, limited_by + " (floor of one case)"
    return PoolPlan(light_slots=light, foam_slots=foam, foam_cores_per_case=per_case,
                    limited_by=limited_by)


# --- settings ------------------------------------------------------------------------

@dataclass
class Settings:
    data_dir: Path
    source_root: Path
    host: str
    port: int
    store: str                          # local | s3
    store_dir: Path
    s3_endpoint: Optional[str]
    s3_bucket: Optional[str]
    s3_region: str
    s3_prefix: str
    s3_access_key: Optional[str]
    s3_secret_key: Optional[str]
    url_ttl_s: int
    max_upload_mb: int
    python: str
    llm_mode: str
    llm_kinds: List[str]
    command_overrides: Optional[Path]
    foam_video: bool
    foam_solve: bool
    foam_max_steps_per_chip: int
    foam_timeout_s: int
    light_timeout_s: int
    design_timeout_s: int
    lease_s: int
    poll_s: float
    light_slots: Optional[int]
    foam_cores: Optional[int]
    foam_cap: int
    foam_mem_gb: float
    foam_disk_gb: float

    @property
    def db_path(self) -> Path:
        return self.data_dir / "queue.sqlite3"

    @property
    def workspaces(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def pidfile(self) -> Path:
        return self.data_dir / "a2c.pid"

    def plan(self, machine: Optional[Machine] = None) -> PoolPlan:
        machine = machine or detect_machine(self.data_dir)
        return plan_pool(machine, light_slots=self.light_slots, foam_cores=self.foam_cores,
                         foam_cap=self.foam_cap, foam_mem_gb=self.foam_mem_gb,
                         foam_disk_gb=self.foam_disk_gb)

    def public(self) -> dict:
        """What is safe to show: never a key, a secret or a token."""
        return {"data_dir": str(self.data_dir), "host": self.host, "port": self.port,
                "store": self.store, "s3_endpoint": self.s3_endpoint,
                "s3_bucket": self.s3_bucket, "llm_mode": self.llm_mode,
                "foam_video": self.foam_video, "foam_solve": self.foam_solve,
                "foam_max_steps_per_chip": self.foam_max_steps_per_chip}


def load(env: Optional[dict] = None) -> Settings:
    """Settings from os.environ, or from `env` alone (tests)."""
    saved = None
    if env is not None:
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
    try:
        data = Path(_env("A2C_DATA_DIR", str(default_data_dir()))).expanduser()
        overrides = _env("A2C_COMMAND_OVERRIDES")
        return Settings(
            data_dir=data,
            source_root=Path(_env("A2C_SOURCE_ROOT", str(ROOT))),
            host=_env("A2C_HOST", "127.0.0.1"),
            port=_env_int("A2C_PORT", 8765),
            store=_env("A2C_STORE", "local"),
            store_dir=Path(_env("A2C_STORE_DIR", str(data / "objects"))).expanduser(),
            s3_endpoint=_env("A2C_S3_ENDPOINT"),
            s3_bucket=_env("A2C_S3_BUCKET"),
            s3_region=_env("A2C_S3_REGION", "us-east-1"),
            s3_prefix=(_env("A2C_S3_PREFIX", "") or "").strip("/"),
            s3_access_key=_env("A2C_S3_ACCESS_KEY_ID") or _env("AWS_ACCESS_KEY_ID"),
            s3_secret_key=_env("A2C_S3_SECRET_ACCESS_KEY") or _env("AWS_SECRET_ACCESS_KEY"),
            url_ttl_s=_env_int("A2C_SIGNED_URL_TTL_S", 3600),
            max_upload_mb=_env_int("A2C_MAX_UPLOAD_MB", 50),
            python=_env("A2C_PYTHON", sys.executable),
            llm_mode=_env("A2C_LLM", "stub"),
            llm_kinds=[k.strip() for k in (_env("A2C_LLM_KINDS", "extract,design") or "")
                       .split(",") if k.strip()],
            command_overrides=Path(overrides) if overrides else None,
            foam_video=_env_bool("A2C_FOAM_VIDEO", False),
            foam_solve=_env_bool("A2C_FOAM_SOLVE", True),
            foam_max_steps_per_chip=_env_int("A2C_FOAM_MAX_STEPS_PER_CHIP", 4),
            foam_timeout_s=_env_int("A2C_FOAM_TIMEOUT_S", 4 * 3600),
            light_timeout_s=_env_int("A2C_LIGHT_TIMEOUT_S", 1800),
            design_timeout_s=_env_int("A2C_DESIGN_TIMEOUT_S", 3 * 3600),
            lease_s=_env_int("A2C_LEASE_S", 120),
            poll_s=_env_float("A2C_POLL_S", 0.5),
            light_slots=_env_int("A2C_LIGHT_SLOTS", None),
            foam_cores=_env_int("A2C_FOAM_CORES_PER_CASE", None),
            foam_cap=_env_int("A2C_FOAM_MAX_PARALLEL", 1),
            foam_mem_gb=_env_float("A2C_FOAM_MEM_GB_PER_CASE", 8.0),
            foam_disk_gb=_env_float("A2C_FOAM_DISK_GB_PER_CASE", 20.0),
        )
    finally:
        if saved is not None:
            os.environ.clear()
            os.environ.update(saved)
