"""
input_pipeline.py
-----------------
Core pipeline logic for discovering, indexing, and validating BIDS datasets.

Steps:
    1. Discover  — Walk the folder tree and find BIDS dataset roots
                   Uses pybids' derivative-dir detection logic directly.
    2. Check     — Evaluate each candidate against DEFAULT_BIDS_RULESET
    3a. Index    — Upsert BIDSDataset rows (description, type, parent link)
                   Uses pybids layout.description for the authoritative dict.
    3b. Files    — Walk each dataset with BIDSLayout, upsert BIDSObject rows
                   Uses layout.get() + f.get_entities() + f.get_metadata().
    4. Validate  — Launch `bids-validator` (Node CLI) as background tasks
    5. Store     — Write validation results into dedicated model columns

pybids is used read-only: we call its battle-tested methods to extract
entities and resolve sidecar inheritance, then discard the layout object.
Nothing is written to pybids' own SQLite database.

The CLI entry point lives in cli.py.
"""

import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.db import async_session_maker, create_all_tables
from db.models import BIDSDataset, BIDSObject, BIDSParticipant
from rules import DEFAULT_BIDS_RULESET, BIDSRuleSet, RuleResult

log = logging.getLogger(__name__)

# Entities stored as first-class columns — everything else → other_entities
_FIRST_CLASS_ENTITIES = frozenset(
    {"subject", "session", "task", "run", "suffix", "datatype", "extension"}
)

# File extensions that map to a specific BIDSObject subclass
_IMAGE_EXTENSIONS = frozenset({".nii", ".nii.gz", ".mgz", ".mgh"})
_DATA_EXTENSIONS  = frozenset({".tsv", ".tsv.gz", ".csv"})
_JSON_EXTENSIONS  = frozenset({".json"})


# ─── pybids lazy import ───────────────────────────────────────────────────────

def _get_bids_layout(path: Path):
    """
    Return a BIDSLayout for `path`.

    Imported lazily so the rest of the pipeline works without pybids
    (e.g. when running tests against pre-built databases).

    validate=False  — we apply our own rules in step 2; pybids validation
                      would silently drop non-conforming files we still want.
    derivatives=False — we discover derivatives ourselves in step 1 so they
                        each get their own BIDSDataset row with a parent_id.
    database_path=None — use an in-memory SQLite; we throw it away after
                         extraction so nothing is persisted by pybids.
    """
    try:
        from bids import BIDSLayout
        from bids.exceptions import BIDSConflictingValuesError
    except ImportError:
        raise ImportError(
            "pybids is required for file-level indexing.\n"
            "  Install with: pip install pybids"
        )
    import json as _json
    import logging
    _log = logging.getLogger(__name__)

    _BIDS_ENTITIES = frozenset({
        # BIDS abbreviations (used in filenames and some sidecars)
        "sub", "ses", "task", "run", "acq", "ce", "dir", "echo", "flip",
        "hemi", "inv", "mt", "part", "proc", "recording", "res", "sample",
        "space", "split", "trc", "stain", "chunk", "density", "atlas",
        "desc", "label", "from", "to", "mod", "seg", "datatype",
        # Full entity names (used in some sidecar column-description JSONs)
        "subject", "session", "acquisition", "direction", "reconstruction",
        "hemisphere", "resolution", "description",
    })

    def _sanitize_sidecars(root: Path) -> int:
        """Rewrite malformed/non-object JSON sidecars and clean invalid entities."""
        n = 0
        for json_file in root.rglob("*.json"):
            raw = json_file.read_bytes()
            try:
                data = _json.loads(raw)
            except Exception:
                # Empty or malformed JSON — replace with empty object so
                # pybids can parse it without throwing JSONDecodeError.
                _log.warning("  replacing malformed/empty JSON with {}: %s", json_file)
                json_file.write_text("{}")
                n += 1
                continue

            # pybids expects JSON sidecars to deserialize to objects. Valid JSON
            # arrays/scalars still break metadata merges, so stub them out.
            if not isinstance(data, dict):
                _log.warning("  replacing non-object JSON with {}: %s", json_file)
                json_file.write_text("{}")
                n += 1
                continue

            dirty = any(
                k in _BIDS_ENTITIES and not isinstance(v, str)
                for k, v in data.items()
            )
            if dirty:
                cleaned = {
                    k: v for k, v in data.items()
                    if not (k in _BIDS_ENTITIES and not isinstance(v, str))
                }
                _log.warning("  sanitized sidecar: %s", json_file)
                json_file.write_text(_json.dumps(cleaned))
                n += 1
        return n

    try:
        layout = BIDSLayout(str(path), validate=False, derivatives=False, database_path=None)
        # Eagerly trigger file-entity loading so any metadata merge issues
        # surface here instead of later in the indexing pass.
        layout.get()
        return layout
    except (BIDSConflictingValuesError, ValueError, OSError, TypeError) as exc:
        _log.warning(
            "pybids error for %s: %s — sanitizing sidecars and retrying",
            path, exc,
        )
        n = _sanitize_sidecars(path)
        _log.warning("  sanitized %d sidecar(s), retrying BIDSLayout …", n)
        try:
            layout = BIDSLayout(str(path), validate=False, derivatives=False, database_path=None)
            layout.get()
            return layout
        except (BIDSConflictingValuesError, ValueError, OSError, TypeError) as exc2:
            raise RuntimeError(
                f"pybids error persists after sanitizing {n} sidecar(s): {exc2}"
            ) from None


# ─── Step 1 + 2: Discovery ────────────────────────────────────────────────────

def _iter_derivative_dirs(deriv_root: Path):
    """
    Yield (name, path) for every derivative dataset under `deriv_root`.

    Ported directly from BIDSLayout._get_derivative_dirs in layout.py:
      - If deriv_root itself has a dataset_description.json it is a single
        pipeline dir → yield it directly.
      - Otherwise treat deriv_root as a container and yield every immediate
        child that has a dataset_description.json.

    This is the battle-tested discovery logic from pybids, extracted as a
    standalone generator that requires no BIDSLayout instance.
    """
    p = deriv_root.resolve()
    if (p / "dataset_description.json").exists():
        yield (p.name, p)
    else:
        for dd in sorted(p.glob("*/dataset_description.json")):
            yield (dd.parent.name, dd.parent)


def discover_datasets(
    root: Path,
    ruleset: BIDSRuleSet = DEFAULT_BIDS_RULESET,
) -> list[tuple[Path, list[RuleResult]]]:
    """
    Walk `root` and return all qualifying BIDS dataset roots.

    Uses pybids' derivative-dir detection pattern for the derivatives/
    subtree (via _iter_derivative_dirs), and rglob for all other locations.

    Each candidate is evaluated against `ruleset`.  Hard-rule failures
    silently skip the directory; soft-rule failures are logged as warnings
    but the dataset is still accepted.

    Returns list of (path, rule_results).
    Note: description is intentionally NOT parsed here — pybids'
    layout.description is used at index time for authoritative parsing.
    """
    log.info("Scanning %s …", root)
    accepted: list[tuple[Path, list[RuleResult]]] = []
    accepted_roots: list[Path] = []

    def _try_accept(candidate: Path) -> None:
        candidate = candidate.resolve()

        # Skip if already covered by an accepted parent root
        if any(candidate != p and p in candidate.parents for p in accepted_roots):
            log.debug("Skipping nested path %s (covered by a parent dataset)", candidate)
            return

        qualifies, results = ruleset.evaluate(candidate)
        if not qualifies:
            blocker = next(r for r in results if r.is_blocker)
            log.warning("Skipping %s — %s", candidate, blocker.message)
            return

        accepted.append((candidate, results))
        accepted_roots.append(candidate)
        log.info("  [+] %s", candidate.name)
        for r in results:
            if not r.passed:
                log.warning("       ! %s", r.message)

    # Accept the root itself if it is a dataset
    if (root / "dataset_description.json").exists():
        _try_accept(root)

    # Accept any non-derivative subdirectory that looks like a dataset
    for desc_path in sorted(root.rglob("dataset_description.json")):
        candidate = desc_path.parent
        if candidate == root:
            continue  # already handled above
        # For derivatives/ subtrees use pybids' own logic (handles nested pipelines)
        if candidate.parent.name == "derivatives":
            for _, deriv_path in _iter_derivative_dirs(candidate.parent):
                _try_accept(deriv_path)
        elif "derivatives" not in candidate.parts:
            _try_accept(candidate)

    return accepted


# ─── Step 3a: Upsert dataset row ──────────────────────────────────────────────

def _resolve_parent(path: Path) -> Path | None:
    """
    If `path` lives inside a derivatives/ subtree, return the nearest ancestor
    directory that contains its own dataset_description.json (the raw parent).
    Returns None for raw datasets or orphaned derivatives.
    """
    parts = path.resolve().parts
    if "derivatives" not in parts:
        return None
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "derivatives":
            candidate = Path(*parts[:i])
            if (candidate / "dataset_description.json").exists():
                return candidate
            break
    return None


async def upsert_dataset(
    session,
    path: Path,
    description: dict,
) -> BIDSDataset:
    """
    Insert or update a BIDSDataset row keyed on root_path.

    `description` is the authoritative dict from pybids layout.description
    (or our own json.loads fallback if pybids is unavailable).

    dataset_type is inferred from the path hierarchy ("raw" | "derivative").
    parent_id is resolved by looking up the parent's root_path in the DB.
    Validation columns are left untouched on update.
    """
    dataset_type = "derivative" if "derivatives" in path.resolve().parts else "raw"
    parent_path = _resolve_parent(path)

    parent_id = None
    if parent_path is not None:
        parent_row = await session.scalar(
            select(BIDSDataset).where(
                BIDSDataset.root_path == str(parent_path.resolve())
            )
        )
        if parent_row is not None:
            parent_id = parent_row.id
        else:
            log.warning(
                "Derivative '%s' has a parent at '%s' not yet in the DB — "
                "parent_id will be NULL until the parent is indexed.",
                path.name, parent_path,
            )

    stmt = (
        pg_insert(BIDSDataset)
        .values(
            id=uuid4(),
            root_path=str(path.resolve()),
            description=description,
            name=description.get("Name", path.name)[:300],
            bids_version=description.get("BIDSVersion", "unknown")[:200],
            dataset_type=dataset_type,
            parent_id=parent_id,
        )
        .on_conflict_do_update(
            index_elements=["root_path"],
            set_={
                "description":    pg_insert(BIDSDataset).excluded.description,
                "name":           pg_insert(BIDSDataset).excluded.name,
                "bids_version":   pg_insert(BIDSDataset).excluded.bids_version,
                "dataset_type":   pg_insert(BIDSDataset).excluded.dataset_type,
                "parent_id":      pg_insert(BIDSDataset).excluded.parent_id,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()

    return await session.scalar(
        select(BIDSDataset).where(BIDSDataset.root_path == str(path.resolve()))
    )


# ─── Step 3b: File-level indexing via pybids ──────────────────────────────────

def _choose_class(extension: str | None) -> str:
    """Map a file extension to the BIDSObject polymorphic identity string."""
    if extension in _IMAGE_EXTENSIONS:
        return "image_file"
    if extension in _DATA_EXTENSIONS:
        return "data_file"
    if extension in _JSON_EXTENSIONS:
        return "json_file"
    return "file"


def _build_object_values(dataset: BIDSDataset, bids_file) -> dict:
    """
    Convert a pybids BIDSFile into a flat dict ready for DB insertion.

    Uses two pybids read methods:
      - f.get_entities()  — filename-parsed entities (battle-tested regex
                            matching against the BIDS schema config)
      - f.get_metadata()  — sidecar inheritance merge (walks directory tree,
                            applies BIDS precedence rules)

    The result maps directly onto BIDSObject columns + other_entities.
    """
    # ── pybids read calls ────────────────────────────────────────────────────
    entities = bids_file.get_entities()           # {name: value, …}
    try:
        metadata = dict(bids_file.get_metadata())     # merged JSON sidecars
    except (TypeError, ValueError):
        metadata = {}

    # ── First-class columns ──────────────────────────────────────────────────
    subject_label = entities.get("subject")       # "01"  (padded str)
    session_label = entities.get("session")

    def _safe_int32(value) -> int | None:
        """Parse integer-like entity labels, returning None when out of int32 range."""
        if value is None:
            return None
        try:
            parsed = int(value)
        except (ValueError, OverflowError, TypeError):
            return None
        if parsed < -2147483648 or parsed > 2147483647:
            return None
        return parsed

    run_raw = entities.get("run")
    run_index = _safe_int32(run_raw)

    extension = entities.get("extension")
    if extension and len(extension) > 64:
        # pybids sometimes returns the full dotted suffix of non-BIDS filenames
        # (e.g. ".724560000_4294967295.nii"). Fall back to just the last component.
        last = "." + extension.rsplit(".", 1)[-1] if "." in extension else extension
        extension = last if len(last) <= 64 else last[:64]

    # ── Overflow: secondary entities + sidecar metadata ──────────────────────
    # Secondary entities (echo, acq, ce, dir, space, …) that didn't get a
    # dedicated column are merged with the full sidecar metadata dict.
    # On PostgreSQL this column is JSONB + GIN indexed, so individual keys
    # remain queryable without adding more columns.
    overflow_entities = {
        k: str(v)
        for k, v in entities.items()
        if k not in _FIRST_CLASS_ENTITIES
    }
    other_entities = {**overflow_entities, **metadata}
    # Recursively sanitize non-JSON-serialisable float values (NaN, Inf)
    # that PostgreSQL rejects as invalid JSON tokens. NaN can appear nested
    # inside dicts or lists inside sidecar metadata values.
    import math as _math

    def _sanitize_json(obj):
        if isinstance(obj, float):
            return None if not _math.isfinite(obj) else obj
        if isinstance(obj, dict):
            return {k: _sanitize_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize_json(v) for v in obj]
        return obj

    other_entities = _sanitize_json(other_entities)

    return dict(
        id=uuid4(),
        dataset_id=dataset.id,
        path=bids_file.path,
        subject=subject_label,
        subject_index=_safe_int32(subject_label) if subject_label and str(subject_label).isdigit() else None,
        session=session_label,
        session_index=_safe_int32(session_label) if session_label and str(session_label).isdigit() else None,
        task=entities.get("task"),
        run=run_index,
        suffix=entities.get("suffix", ""),
        datatype=entities.get("datatype"),
        extension=extension,
        other_entities=other_entities,
        filename=bids_file.filename,
        class_=_choose_class(extension),
        is_remote=False,
    )


async def index_dataset_files(
    session,
    dataset: BIDSDataset,
    path: Path,
    batch_size: int = 500,
) -> int:
    """
    Walk `path` with BIDSLayout and upsert one BIDSObject row per file.

    Existing rows are updated in place (ON CONFLICT on path).  Files that
    have been removed from disk since the last run are NOT deleted here —
    use a separate cleanup pass for that.

    Returns the number of files upserted.
    """
    log.info("  [pybids] Indexing files in '%s' …", dataset.name)

    layout = _get_bids_layout(path)

    # layout.description is pybids' authoritative parsed dataset_description
    # dict — already validated against the BIDS schema config.
    # We also use it to refresh the description column in case it changed.
    await session.execute(
        update(BIDSDataset)
        .where(BIDSDataset.id == dataset.id)
        .values(description=layout.description)
    )

    bids_files = layout.get()   # list of pybids BIDSFile objects, all file types
    log.info("  [pybids] Found %d file(s)", len(bids_files))

    total = 0
    batch: list[dict] = []

    for bids_file in bids_files:
        try:
            values = _build_object_values(dataset, bids_file)
        except Exception as exc:
            log.warning("  [skip] %s — %s", bids_file.path, exc)
            continue

        batch.append(values)

        if len(batch) >= batch_size:
            await _flush_objects(session, batch)
            total += len(batch)
            batch = []

    if batch:
        await _flush_objects(session, batch)
        total += len(batch)

    await session.commit()
    log.info("  [db] Upserted %d file(s) for '%s'", total, dataset.name)
    return total


async def _flush_objects(session, batch: list[dict]) -> None:
    """Bulk-upsert a batch of BIDSObject value dicts (keyed on path)."""
    stmt = (
        pg_insert(BIDSObject)
        .values(batch)
        .on_conflict_do_update(
            index_elements=["path"],
            set_={
                "subject":        pg_insert(BIDSObject).excluded.subject,
                "subject_index":  pg_insert(BIDSObject).excluded.subject_index,
                "session":        pg_insert(BIDSObject).excluded.session,
                "session_index":  pg_insert(BIDSObject).excluded.session_index,
                "task":           pg_insert(BIDSObject).excluded.task,
                "run":            pg_insert(BIDSObject).excluded.run,
                "suffix":         pg_insert(BIDSObject).excluded.suffix,
                "datatype":       pg_insert(BIDSObject).excluded.datatype,
                "extension":      pg_insert(BIDSObject).excluded.extension,
                "other_entities": pg_insert(BIDSObject).excluded.other_entities,
                "class_":         pg_insert(BIDSObject).excluded.class_,
                "is_remote":      pg_insert(BIDSObject).excluded.is_remote,
            },
        )
    )
    await session.execute(stmt)


# ─── Step 4: Background BIDS validation ───────────────────────────────────────

def _validator_command(path: Path) -> list[str]:
    """
    Build the bids-validator command appropriate for the current environment.

    Inside Docker (BIDS_DATA_PATH is set):
        The data is mounted at /data in both the bids-sql and bids-validator
        containers, so we invoke it via `docker compose run` against the
        /data path.  The bids-validator sidecar uses the pre-built official
        image — no npm install needed.

    Outside Docker (local dev, bids-validator on PATH):
        Fall back to calling the binary directly.
    """
    import os
    if os.getenv("BIDS_VALIDATOR_URL"):
        # Running inside compose: delegate to the bids-validator sidecar.
        # Map the host path back to its /data mount point inside the container.
        data_root = Path(os.getenv("BIDS_DATA_PATH", "/data")).resolve()
        try:
            rel = path.resolve().relative_to(data_root.resolve())
            container_path = Path("/data") / rel
        except ValueError:
            container_path = path  # path is already absolute inside container
        return [
            "docker", "compose", "run", "--rm", "--no-deps",
            "bids-validator", str(container_path), "--json",
        ]
    return ["bids-validator", str(path), "--json"]


async def launch_validator(path: Path) -> asyncio.subprocess.Process:
    """
    Start bids-validator as a non-blocking subprocess.

    In Docker: delegates to the bids-validator sidecar via docker compose run.
    Locally: calls the bids-validator binary directly (must be on PATH).

    All datasets are launched before any result is awaited so validation
    runs in parallel across all discovered datasets.
    """
    cmd = _validator_command(path)
    log.info("  [→] bids-validator started for %s  (%s)", path.name, " ".join(cmd[:4]))
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _parse_validator_version(stderr_text: str) -> str | None:
    match = re.search(r"bids-validator@([\d.]+)", stderr_text)
    return match.group(1) if match else None


async def collect_validation_result(proc: asyncio.subprocess.Process, path: Path) -> dict:
    """
    Await a running validator process and return a structured result dict with
    keys: status, errors, warnings, validator_version, validated_at.
    """
    import json
    stdout, stderr = await proc.communicate()
    validated_at = datetime.now(tz=timezone.utc).isoformat()
    validator_version = _parse_validator_version(stderr.decode())

    if proc.returncode not in (0, 1):
        log.error("bids-validator error for %s (exit %d): %s",
                  path.name, proc.returncode, stderr.decode().strip())
        return {"status": "error", "errors": [], "warnings": [],
                "validator_version": validator_version, "validated_at": validated_at,
                "stderr": stderr.decode()}

    try:
        raw = json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"status": "error", "errors": [], "warnings": [],
                "validator_version": validator_version, "validated_at": validated_at,
                "raw_output": stdout.decode()}

    errors   = raw.get("issues", {}).get("errors", [])
    warnings = raw.get("issues", {}).get("warnings", [])
    status   = "valid" if not errors else "invalid"
    log.info("  [✓] %s — %s (%d error(s), %d warning(s))",
             path.name, status, len(errors), len(warnings))
    return {"status": status, "errors": errors, "warnings": warnings,
            "validator_version": validator_version or raw.get("version"),
            "validated_at": validated_at}


# ─── Step 5: Store validation results ─────────────────────────────────────────

async def store_validation_result(session, dataset: BIDSDataset, result: dict) -> None:
    """Write validation results into the dedicated columns on BIDSDataset."""
    validated_at = None
    if ts := result.get("validated_at"):
        validated_at = datetime.fromisoformat(ts)

    await session.execute(
        update(BIDSDataset)
        .where(BIDSDataset.id == dataset.id)
        .values(
            validation_status=result.get("status"),
            validation_errors=result.get("errors"),
            validation_warnings=result.get("warnings"),
            validator_version=result.get("validator_version"),
            validated_at=validated_at,
        )
    )
    await session.commit()
    log.info("  [db] Stored validation for '%s' — status: %s",
             dataset.name, result.get("status"))


# ─── Step 3c: participants.tsv indexing ──────────────────────────────────────

# Known column names and how they map to BIDSParticipant columns
_PARTICIPANT_COLUMN_MAP = {
    "participant_id": "participant_id",
    "age": "age",
    "sex": "sex",
    "gender": "sex",           # common alias
    "handedness": "handedness",
    "diagnosis": "diagnosis",
    "group": "diagnosis",      # common alias
    "pathology": "diagnosis",  # common alias
}


async def index_participants_tsv(session, dataset: BIDSDataset, dataset_path: Path) -> int:
    """
    Parse participants.tsv from a dataset root and upsert rows into bids_participants.

    Returns the number of rows upserted.
    """
    tsv_path = dataset_path / "participants.tsv"
    if not tsv_path.exists():
        return 0

    try:
        import pandas as pd
        df = pd.read_csv(tsv_path, sep="\t", na_values="n/a", dtype=str)
    except Exception as exc:
        log.warning("  [participants] Could not read %s: %s", tsv_path, exc)
        return 0

    if "participant_id" not in df.columns:
        log.warning("  [participants] %s has no participant_id column — skipped", tsv_path)
        return 0

    rows: list[dict] = []
    known_cols = set(_PARTICIPANT_COLUMN_MAP.keys())
    for _, row in df.iterrows():
        participant_id = str(row["participant_id"]).strip()
        if not participant_id:
            continue

        def _clean(val) -> str | None:
            if val is None:
                return None
            s = str(val).strip()
            return None if s.lower() in ("n/a", "nan", "", "none") else s

        age_raw = _clean(row.get("age"))
        age: float | None = None
        try:
            age = float(age_raw) if age_raw is not None else None
        except ValueError:
            pass

        sex = (_clean(row.get("sex") or row.get("gender")) or "")[:50] or None
        handedness = (_clean(row.get("handedness")) or "")[:50] or None
        diagnosis_raw = _clean(
            row.get("diagnosis") or row.get("group") or row.get("pathology")
        )
        diagnosis = diagnosis_raw.lower()[:200] if diagnosis_raw else None

        extra_cols = {
            col: _clean(row.get(col))
            for col in df.columns
            if col not in known_cols
        }
        extra = {k: v for k, v in extra_cols.items() if v is not None} or None

        rows.append(dict(
            id=uuid4(),
            dataset_id=dataset.id,
            participant_id=participant_id,
            age=age,
            sex=sex,
            handedness=handedness,
            diagnosis=diagnosis,
            extra=extra,
        ))

    if not rows:
        return 0

    # Deduplicate on (dataset_id, participant_id) — some TSV files have duplicate
    # participant_id rows which cause CardinalityViolationError in ON CONFLICT DO UPDATE.
    seen: set[tuple] = set()
    deduped = []
    for r in rows:
        key = (r["dataset_id"], r["participant_id"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    rows = deduped

    stmt = (
        pg_insert(BIDSParticipant)
        .values(rows)
        .on_conflict_do_update(
            constraint="uq_participant_per_dataset",
            set_={
                "age":        pg_insert(BIDSParticipant).excluded.age,
                "sex":        pg_insert(BIDSParticipant).excluded.sex,
                "handedness": pg_insert(BIDSParticipant).excluded.handedness,
                "diagnosis":  pg_insert(BIDSParticipant).excluded.diagnosis,
                "extra":      pg_insert(BIDSParticipant).excluded.extra,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()
    log.info(
        "  [participants] Upserted %d row(s) from %s", len(rows), tsv_path.name
    )
    return len(rows)


# ─── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    input_folder: str,
    skip_validation: bool = False,
    ruleset: BIDSRuleSet = DEFAULT_BIDS_RULESET,
) -> None:
    root = Path(input_folder).expanduser().resolve()
    if not root.is_dir():
        log.error("'%s' is not a directory", root)
        sys.exit(1)

    # ── Steps 1 + 2: Discover and rule-check ─────────────────────────────────
    log.info("=== Steps 1+2: Discovering BIDS datasets under %s ===", root)
    candidates = discover_datasets(root, ruleset=ruleset)
    if not candidates:
        log.warning("No valid BIDS datasets found under %s", root)
        return
    log.info("Found %d dataset(s).", len(candidates))

    await create_all_tables()

    # ── Step 3a: Upsert dataset-level rows ───────────────────────────────────
    # Raw datasets first so derivatives can resolve parent_id in the same pass.
    log.info("=== Step 3a: Indexing dataset metadata ===")
    raw_first = sorted(candidates, key=lambda c: "derivatives" in c[0].resolve().parts)

    indexed: list[tuple[Path, BIDSDataset]] = []
    async with async_session_maker() as session:
        for path, _ in raw_first:
            # Use pybids to parse dataset_description — same parser it uses
            # internally, so BIDS-specific fields (GeneratedBy, DatasetLinks,
            # etc.) are handled correctly.
            try:
                layout = _get_bids_layout(path)
                description = layout.description
            except Exception:
                import json as _json
                description = _json.loads(
                    (path / "dataset_description.json").read_text(encoding="utf-8")
                )
                log.warning("  pybids failed to load '%s', falling back to raw JSON", path.name)

            dataset = await upsert_dataset(session, path, description)
            indexed.append((path, dataset))
            log.info("  [db] '%s'  type=%s  id=%s", dataset.name, dataset.dataset_type, dataset.id)

    # ── Step 3b: File-level indexing ─────────────────────────────────────────
    log.info("=== Step 3b: Indexing files via pybids ===")
    async with async_session_maker() as session:
        for path, dataset in indexed:
            await index_dataset_files(session, dataset, path)

    # ── Step 3c: participants.tsv ─────────────────────────────────────────────
    log.info("=== Step 3c: Indexing participants.tsv ===")
    async with async_session_maker() as session:
        for path, dataset in indexed:
            await index_participants_tsv(session, dataset, path)

    if skip_validation:
        log.info("Skipping validation (--no-validate passed).")
        log.info("=== Done. %d dataset(s) indexed. ===", len(indexed))
        return

    # ── Step 4: Launch bids-validator background processes ───────────────────
    log.info("=== Step 4: Launching bids-validator ===")
    procs: list[tuple[Path, asyncio.subprocess.Process]] = []
    for path, _ in indexed:
        try:
            procs.append((path, await launch_validator(path)))
        except FileNotFoundError:
            log.error(
                "bids-validator not found on PATH.\n"
                "  Install with:  npm install -g bids-validator\n"
                "  Skipping validation."
            )
            log.info("=== Done (no validation). %d dataset(s) indexed. ===", len(indexed))
            return

    # ── Step 5: Collect results and write to DB ───────────────────────────────
    log.info("=== Step 5: Collecting validation results ===")
    results = await asyncio.gather(
        *[collect_validation_result(proc, path) for path, proc in procs]
    )
    async with async_session_maker() as session:
        for (_, dataset), result in zip(indexed, results):
            refreshed = await session.get(BIDSDataset, dataset.id)
            await store_validation_result(session, refreshed, result)

    log.info("=== Pipeline complete. %d dataset(s) processed. ===", len(indexed))
