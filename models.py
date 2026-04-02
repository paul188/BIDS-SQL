from __future__ import annotations

import json
from datetime import datetime
from functools import cached_property
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, FetchedValue, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from upath import UPath

from base import Base
from utils import BIDSMetadata


class BIDSDataset(Base):
    """
    Represents the parent directory of a BIDS dataset
    with a dataset_description.json at the root.
    """
    __tablename__ = 'bids_datasets'

    # Fix (2): use Python-side uuid4 — func.gen_random_uuid() was broken (func never imported)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Root filesystem path (absolute or relative to a data storage root)
    root_path: Mapped[str] = mapped_column(String, unique=True, index=True)

    # Store the parsed dataset_description.json here for instant access
    description: Mapped[Dict] = mapped_column(JSON, nullable=False)

    # Metadata derived from description (for fast filtering)
    name: Mapped[str] = mapped_column(String(100), index=True)
    bids_version: Mapped[str] = mapped_column(String(20))
    dataset_type: Mapped[str] = mapped_column(String(20), default="raw", index=True)  # "raw" | "derivative"

    # Self-referential parent/derivative hierarchy
    # A derivative dataset (e.g. derivatives/fmriprep/) points back to the
    # raw dataset it was generated from.  Raw datasets have parent_id = NULL.
    parent_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("bids_datasets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent: Mapped[Optional["BIDSDataset"]] = relationship(
        "BIDSDataset",
        back_populates="derivatives",
        foreign_keys="[BIDSDataset.parent_id]",
        remote_side="[BIDSDataset.id]",
    )
    derivatives: Mapped[List["BIDSDataset"]] = relationship(
        "BIDSDataset",
        back_populates="parent",
        foreign_keys="[BIDSDataset.parent_id]",
    )

    # Validation results — populated after the background bids-validator run
    # (NOT the discovery rules, which are intentionally lax)
    validation_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default=None)
    validation_errors: Mapped[Optional[Dict]] = mapped_column(JSON, nullable=True, default=None)
    validation_warnings: Mapped[Optional[Dict]] = mapped_column(JSON, nullable=True, default=None)
    validator_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    # One Dataset → Many BIDSObjects
    objects: Mapped[List["BIDSObject"]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<BIDSDataset(name='{self.name}', root='{self.root_path}')>"


class BIDSObject(Base):
    """
    A single file or directory entry in a BIDS dataset.

    Hot entities (subject, session, task, run, suffix) are stored in
    first-class indexed columns for fast filtering.

    Subject and session carry both a label column (the zero-padded string
    "01", "02", …) and an index column (the bare integer 1, 2, …).
    This lets you do exact label matching AND numeric range queries
    (e.g. subject_index > 10) without any string-to-int casting in SQL.

    All other entities live in the `other_entities` JSON/JSONB column
    (the 'chaos bucket').
    """
    __tablename__ = 'bids_objects'

    # Fix (2): same uuid4 default as BIDSDataset
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    path: Mapped[str] = mapped_column(String, unique=True, index=True)

    # ── Core entities — first-class columns ──────────────────────────────────

    # Fix (1): subject and session get both a label (padded string "01") and
    # an integer index (1) so numeric range queries work correctly.
    subject: Mapped[Optional[str]] = mapped_column(String, index=True)      # "01"
    subject_index: Mapped[Optional[int]] = mapped_column(Integer, index=True)  # 1

    session: Mapped[Optional[str]] = mapped_column(String, index=True)      # "01"
    session_index: Mapped[Optional[int]] = mapped_column(Integer, index=True)  # 1

    task: Mapped[Optional[str]] = mapped_column(String, index=True)
    run: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    suffix: Mapped[str] = mapped_column(String, index=True)
    datatype: Mapped[Optional[str]] = mapped_column(String(64), index=True)   # "func", "anat", "dwi", …
    extension: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # ".nii.gz", ".json", …

    # ── Chaos bucket — everything else ───────────────────────────────────────
    # Secondary entities: echo, acq, ce, dir, space, res, hemi, …
    # On PostgreSQL this should be JSONB (swap JSON → JSONB + add GIN index
    # in the Alembic migration to enable fast key/value lookups inside it).
    other_entities: Mapped[Dict] = mapped_column(JSON, default=dict)

    # ── Dataset link ─────────────────────────────────────────────────────────
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey('bids_datasets.id'), index=True)
    dataset: Mapped["BIDSDataset"] = relationship(back_populates="objects")

    # ── Polymorphic identity ──────────────────────────────────────────────────
    class_: Mapped[str] = mapped_column(String(20))
    __mapper_args__ = {
        "polymorphic_on": "class_",
        "polymorphic_identity": "base_object",
    }

    # ── Associations (Fix 6: added `kind`) ───────────────────────────────────
    associations: Mapped[List["ObjectAssociation"]] = relationship(
        primaryjoin="BIDSObject.id == ObjectAssociation.src_id",
        back_populates="source",
    )

    filename: Mapped[str] = mapped_column(
        String,
        server_default=FetchedValue(),  # computed by a SQL trigger/migration
    )

    # ── Metadata access (Fix 3) ───────────────────────────────────────────────

    @property
    def metadata(self) -> BIDSMetadata:
        """
        Return all secondary metadata as a BIDSMetadata dict.

        BIDSMetadata is a dict subclass that raises descriptive KeyErrors
        including the source file path, which helps debugging missing fields.
        """
        md = BIDSMetadata(self.path)
        md.update(self.other_entities or {})  # was: self._metadata_blob (never defined)
        return md

    @metadata.setter
    def metadata(self, value: Dict) -> None:
        """Persist metadata by writing it into the `other_entities` column."""
        self.other_entities = dict(value)

    def get_metadata(self) -> BIDSMetadata:
        """PyBIDS-compatible access — delegates to the `metadata` property."""
        return self.metadata


class BIDSFile(BIDSObject):
    """A generic file in a BIDS dataset."""

    __mapper_args__ = {"polymorphic_identity": "file"}

    # Fix (7): cached_property so UPath is only constructed once per instance
    # instead of on every attribute access. Safe for read-heavy ORM objects.
    @cached_property
    def _path(self) -> UPath:
        return UPath(self.path)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} filename='{self.path}'>"

    def __fspath__(self) -> str:
        """Allow direct use with os.path functions."""
        return self.path


class BIDSImageFile(BIDSFile):
    """A neuroimaging data file (.nii, .nii.gz, …)."""

    __mapper_args__ = {"polymorphic_identity": "image_file"}

    def get_image(self, **kwargs):
        """Return the file as a NiBabel image object."""
        try:
            import nibabel as nb
            return nb.load(self.path, **kwargs)
        except Exception as e:
            raise ValueError(
                f"'{self.path}' does not appear to be a format NiBabel can read."
            ) from e


class BIDSDataFile(BIDSFile):
    """
    A BIDS tabular data file (.tsv, .tsv.gz).

    Covers events files, physio/stim recordings, participants.tsv,
    scans.tsv, and any other tab-separated file in the dataset.
    """

    __mapper_args__ = {"polymorphic_identity": "data_file"}

    def get_df(self, enforce_dtypes: bool = True, include_timing: bool = True,
               adjust_onset: bool = False, **pd_args):
        """
        Return the file contents as a pandas DataFrame.

        Parameters
        ----------
        enforce_dtypes:
            When True, forces BIDS-mandated string dtypes for ID columns
            (participant_id, subject_id, session_id) so that "01" is never
            silently coerced to the integer 1.
        include_timing:
            For dense timeseries (.tsv.gz) files: if True, prepends an
            'onset' column derived from SamplingFrequency in the sidecar.
        adjust_onset:
            If True, shifts the onset by the 'StartTime' sidecar value.
            Ignored when include_timing=False or for plain .tsv files.
        **pd_args:
            Any additional keyword arguments forwarded to pd.read_csv().
        """
        import numpy as np
        import pandas as pd

        dtype = (
            {"participant_id": str, "subject_id": str, "session_id": str}
            if enforce_dtypes
            else None
        )

        # physio/stim files have no header row
        suffix = (self.other_entities or {}).get("suffix") or self.suffix
        header = None if suffix in {"physio", "stim"} else "infer"

        df = pd.read_csv(
            self.path,
            sep="\t",
            na_values="n/a",
            dtype=dtype,
            header=header,
            **pd_args,
        )

        # Dense timeseries: reconstruct column names and optionally add onset
        if str(self._path).endswith(".tsv.gz"):
            md = self.get_metadata()
            df.columns = md["Columns"]
            if include_timing:
                onsets = np.arange(len(df)) / md["SamplingFrequency"]
                if adjust_onset:
                    onsets += md["StartTime"]
                df.insert(0, "onset", onsets)

        return df


class BIDSJSONFile(BIDSFile):
    """A JSON sidecar or metadata file."""

    __mapper_args__ = {"polymorphic_identity": "json_file"}

    def get_dict(self) -> dict:
        """Return the file contents as a Python dict."""
        d = json.loads(self.get_json())
        if not isinstance(d, dict):
            raise ValueError(
                f"Expected a JSON object in {self.path}, got {type(d).__name__}"
            )
        return d

    def get_json(self) -> str:
        """Return the raw JSON string."""
        with open(self.path, "r") as f:
            return f.read()


class ObjectAssociation(Base):
    """
    A directed association between two BIDSObjects.

    Fix (6): added `kind` so callers can distinguish association types —
    e.g. "Metadata" (image ↔ JSON sidecar), "IntendedFor" (image ↔ fmap),
    "Events" (bold ↔ events.tsv), "Child" (parent ↔ derivative).

    The triple (src_id, dst_id, kind) is the primary key, matching the
    pybids FileAssociation table structure.
    """
    __tablename__ = "object_associations"

    src_id: Mapped[UUID] = mapped_column(
        ForeignKey("bids_objects.id", ondelete="CASCADE"), primary_key=True
    )
    dst_id: Mapped[UUID] = mapped_column(
        ForeignKey("bids_objects.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(50), primary_key=True)

    source: Mapped["BIDSObject"] = relationship(
        foreign_keys=[src_id],
        primaryjoin="ObjectAssociation.src_id == BIDSObject.id",
        back_populates="associations",
    )
    target: Mapped["BIDSObject"] = relationship(
        foreign_keys=[dst_id],
        primaryjoin="ObjectAssociation.dst_id == BIDSObject.id",
    )

    def __repr__(self) -> str:
        return f"<ObjectAssociation {self.src_id} --[{self.kind}]--> {self.dst_id}>"
