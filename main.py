#!/usr/bin/env python3
r"""Generate files for a Discord personal-data erasure request.

This script analyzes a Discord data container stored locally as an extracted
directory or a ZIP archive to identify the messages and prepares the files
needed to submit a targeted erasure request to Discord.

The script is intended for users who want to request the deletion of messages
associated with their Discord account without manually collecting every
message identifier. It produces a list of unique message references and a draft
email that summarizes the request.

The analysis is performed entirely on the local computer. The script does not
connect to Discord, send an email, upload data, delete messages, or
submit a request automatically. The generated files must be sent by the user.

The Discord data container may be:

* an extracted Discord data directory;
* a ZIP archive containing the data package.

The container must contain usable message transcripts in JSON or CSV format.
When available, account information found in the package is used to prefill
the draft email. Values provided explicitly on the command line take
precedence over values discovered in the package.

The script generates two files:

`request_attachment.txt`
Contains one unique Discord message reference per line, using the
`server_id:channel_id:message_id` format. The file contains identifiers only. It
does not include message contents or attachments.

`request_message.txt`
Contains a draft personal-data erasure request addressed to Discord. The
draft includes the available account information, the number of messages
concerned, the number of affected channels, and instructions describing
the attached identifier list.

Missing information is represented by visible placeholders in the draft
email. Review and complete these placeholders before sending the request.

## Command-line arguments

`--data, --data-source <path>`
Path to the Discord data container to analyze. The path may identify an
extracted directory or a ZIP archive.

```
The default is ``./data/discord_data`` or ``./data/discord_data.zip``, resolved
from the current working directory.
```

`--o, --output-dir <path>`
Directory in which to create `request_attachment.txt` and
`request_message.txt`.

```
The default is ``./data``, resolved from the current working directory.
```

`--full-name <firstname> <lastname>`
Full name to include in the draft erasure request. When omitted, the
generated email contains extracted Discord username.

`--username USERNAME`
Discord username to include in the draft email. This value takes
precedence over any username discovered in the data container.

`--force`
Replace existing output files. Without this option, the script stops when
either destination file already exists, preventing accidental replacement
of a previously prepared request.

`--dry-run`
Analyze the container and report the results without creating or replacing
output files. Use this option to validate the content before generating
the request.

## Examples

Print help text:

.. code-block:: console

```
python main.py -h
```

Analyze the default container and write the files to the default output
directory:

.. code-block:: console

```
python main.py
```

Analyze an extracted data container at a specific location:

.. code-block:: console

```
python main.py --data-source ./exports/discord_data
```

Analyze a ZIP archive and write the results to another directory:

.. code-block:: console

```
python main.py \
    --data-source ./exports/discord_data.zip \
    --output-dir ./requests/discord
```

Provide the account information required for a complete draft email:

.. code-block:: console

```
python main.py \
    --data-source ./exports/discord_data.zip \
    --full-name "Jane Doe"
```

Inspect the container without writing any files:

.. code-block:: console

```
python main.py \
    --data-source ./exports/discord_data.zip \
    --dry-run
```

Regenerate the files when outputs from an earlier run already exist:

.. code-block:: console

```
python main.py \
    --data-source ./exports/discord_data.zip \
    --output-dir ./requests/discord \
    --force
```

After generation, review `request_message.txt`, complete any bracketed fields,
attach `request_attachment.txt`, and send the request using the appropriate
recipient. Do not include a Discord password, authentication token,
multi-factor authentication code, or unrelated contents from the data
container.

Copyright 2026-present, Joseph Garnier
All rights reserved.

See LICENSE.md file for more information.
"""  # noqa: CPY001

# Future library
from __future__ import annotations

# Standard library
import argparse
import csv
import json
import logging
import re
import sys
import zipfile
from collections.abc import Generator, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import date
from io import TextIOWrapper
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Final, Self, TextIO, override

if TYPE_CHECKING:
    # Standard library
    from collections.abc import Sequence
    from types import TracebackType

###############################################################################
### Configuration constants
###############################################################################
APPLICATION_NAME: Final[str] = "Discord Purger"

LOG_DIRECTORY: Final[Path] = Path("./logs")
LOG_FILENAME: Final[str] = "discord-purger.log"

DISCORD_DATA_DIRECTORY: Final[Path] = Path("./data/discord_data")
DISCORD_DATA_ARCHIVE_FILE: Final[Path] = Path("./data/discord_data.zip")

OUTPUT_DIRECTORY: Final[Path] = Path("./data")
OUTPUT_MESSAGE_FILENAME: Final[str] = "request_message.txt"
OUTPUT_ATTACHMENT_FILENAME: Final[str] = "request_attachment.txt"

ACCOUNT_DIRECTORY_NAMES: Final[tuple[str, ...]] = (
    "account",
    "compte",
    "cuenta",
    "konto",
)

# Discord snowflakes are unsigned decimal identifiers. Keeping them as strings
# avoids accidental formatting or precision changes outside Python.
SNOWFLAKE_PATTERN = re.compile(r"[0-9]{15,20}\Z")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")

###############################################################################
### Global variables
###############################################################################
logger = logging.getLogger(__name__)


###############################################################################
# Exceptions
###############################################################################
class DiscordPurgerError(Exception):
    """Base class for expected application errors."""


class InvalidContainerError(DiscordPurgerError):
    """Raised when the supplied data container cannot be read."""


class NoMessagesFoundError(DiscordPurgerError):
    """Raised when no usable Discord message identifiers are found."""


class OutputFileExistsError(DiscordPurgerError):
    """Raised when an output already exists and --force was not supplied."""


###############################################################################
# Discord data access
###############################################################################
type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None


@dataclass(frozen=True, slots=True)
class ContainerEntry:
    """A file inside either a directory-backed or ZIP-backed container."""

    logical_path: PurePosixPath
    filesystem_path: Path | None = None
    archive_info: zipfile.ZipInfo | None = None


class DiscordDataContainer(AbstractContextManager["DiscordDataContainer"]):
    """Provide uniform, read-only access to a directory or ZIP discord data container."""

    def __init__(self, data_container: Path) -> None:
        """Initialize access to a Discord data container.

        The path is resolved immediately and validated when the context manager
        is entered.

        Args:
            data_container (Path): Directory or ZIP archive that contains the
              exported Discord data.
        """
        self._data_container_path = data_container.resolve()
        self._root_directory: Path | None = None
        self._archive_file: zipfile.ZipFile | None = None
        self._entries: tuple[ContainerEntry, ...] = ()

    @override
    def __enter__(self) -> Self:
        """Open the configured directory or ZIP archive.

        Directory entries are scanned recursively. ZIP entries are indexed
        without extracting their contents.

        Returns:
            Self: Open container whose entries are available through
              :attr:`entries`.

        Raises:
            InvalidContainerError: The path is not a readable directory or valid
              ZIP archive.
        """
        path = self._data_container_path
        if path.is_dir():
            self._root_directory = path
            self._entries = tuple(self._directory_entries())
        elif path.is_file() and zipfile.is_zipfile(path):
            try:
                self._archive_file = zipfile.ZipFile(path, mode="r")
                self._entries = tuple(self._archive_entries())
            except (OSError, zipfile.BadZipFile) as exc:
                msg = f"Invalid ZIP archive: {path}"
                raise InvalidContainerError(msg) from exc
        else:
            msg = f"The container must be an existing directory or a valid ZIP archive: {path}"
            raise InvalidContainerError(msg)
        return self

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backing ZIP archive, if one is open."""
        if self._archive_file is not None:
            self._archive_file.close()

    @property
    def entries(self) -> tuple[ContainerEntry, ...]:
        """The safe, regular files exposed by the open container."""
        return self._entries

    def _directory_entries(self) -> Iterator[ContainerEntry]:
        """Yield safe regular files beneath the container root.

        Inaccessible files and symbolic links that resolve outside the root are
        logged and skipped.

        Yields:
            ContainerEntry: Directory-backed entries in sorted path order.

        Raises:
            InvalidContainerError: The root directory cannot be scanned.
        """
        assert self._root_directory is not None
        root = self._root_directory

        try:
            candidates = sorted(root.rglob("*"))
        except OSError as exc:
            msg = f"Unable to scan directory: {root}"
            raise InvalidContainerError(msg) from exc

        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
            except OSError as exc:
                logger.warning("File skipped because it is inaccessible: %s", candidate)
                logger.debug("Error details", exc_info=exc)
                continue

            # Do not follow a symlink out of the container directory.
            if not resolved.is_relative_to(root):
                logger.warning("External symbolic link skipped: %s", candidate)
                continue

            logical_path = PurePosixPath(candidate.relative_to(root).as_posix())
            yield ContainerEntry(
                logical_path=logical_path,
                filesystem_path=resolved,
            )

    def _archive_entries(self) -> Iterator[ContainerEntry]:
        """Yield safe regular files from the open ZIP archive.

        Directory entries and paths that are absolute or contain a parent
        traversal component are logged and skipped.

        Yields:
            ContainerEntry: ZIP-backed entries sorted by archive filename.
        """
        assert self._archive_file is not None

        infos = sorted(
            self._archive_file.infolist(),
            key=lambda item: item.filename,
        )
        for info in infos:
            if info.is_dir():
                continue

            logical_path = PurePosixPath(info.filename)
            if logical_path.is_absolute() or ".." in logical_path.parts:
                logger.warning("ZIP entry with an unsafe path skipped: %s", info.filename)
                continue

            yield ContainerEntry(
                logical_path=logical_path,
                archive_info=info,
            )

    @contextmanager
    def open_text(self, entry: ContainerEntry) -> Generator[TextIO]:
        """Open a container entry as UTF-8 text.

        The stream accepts an optional UTF-8 byte-order mark. ZIP members are
        read directly from the archive without extraction.

        Args:
            entry (ContainerEntry): Directory-backed or ZIP-backed entry
              to open.

        Yields:
            TextIO: Readable text stream for the entry.

        Raises:
            InvalidContainerError: The entry has no usable backing file or ZIP
              member.
            OSError: A directory-backed entry cannot be opened.
        """
        if entry.filesystem_path is not None:
            with entry.filesystem_path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as stream:
                yield stream
                return

        if self._archive_file is None or entry.archive_info is None:
            msg = f"Invalid internal entry: {entry.logical_path.as_posix()}"
            raise InvalidContainerError(msg)

        with (
            self._archive_file.open(entry.archive_info, mode="r") as binary_stream,
            TextIOWrapper(binary_stream, encoding="utf-8-sig", newline="") as stream,
        ):
            yield stream

    # -------------------------------------------------------------------------
    def load_json(self, entry: ContainerEntry) -> JSONValue:
        """Decode a container entry as JSON.

        Args:
            entry (ContainerEntry): JSON entry to read.

        Returns:
            JSONValue: Decoded JSON value.

        Raises:
            InvalidContainerError: The entry cannot be opened from the
              container.
            OSError: The entry cannot be read.
            UnicodeError: The entry is not valid UTF-8 text.
            json.JSONDecodeError: The entry does not contain valid JSON.
        """
        with self.open_text(entry) as stream:
            return json.load(stream)


###############################################################################
# Discord container parsing helpers
###############################################################################
def normalize_key(value: object) -> str:
    """Normalize a field name for tolerant matching.

    The value is converted to text, case-folded, and stripped of every
    character except ASCII letters and digits.

    Args:
        value (object): Field name or value to normalize.

    Returns:
        str: Normalized lowercase key.
    """
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def as_snowflake(value: object) -> str | None:
    """Convert a value to a validated Discord snowflake string.

    Integer values and stripped strings are accepted. Boolean values and
    values outside the 15-to-20-digit decimal format are rejected.

    Args:
        value (object): Candidate Discord identifier.

    Returns:
        str | None: Validated identifier, or ``None`` when invalid.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        return None

    return candidate if SNOWFLAKE_PATTERN.fullmatch(candidate) else None


def clean_single_line(value: object) -> str | None:
    """Convert a supported value to trimmed single-line text.

    Runs of whitespace are collapsed to one space. Empty strings, boolean
    values, and values other than strings or integers are rejected.

    Args:
        value (object): Value to sanitize.

    Returns:
        str | None: Sanitized text, or ``None`` when no usable text remains.
    """
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    candidate = (" ").join(str(value).split())
    return candidate or None


def normalized_mapping(record: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """Create a normalized-key view of a mapping.

    Later entries replace earlier entries when their keys normalize to the
    same value.

    Args:
        record (Mapping[str, JSONValue]): Mapping whose keys are normalized.

    Returns:
        dict[str, JSONValue]: Values indexed by normalized keys.
    """
    return {normalize_key(key): value for key, value in record.items()}


def is_beneath_named_directory(path: PurePosixPath, *directory_names: str) -> bool:
    """Check whether a path is beneath any of the specified directories.

    Matching is case-insensitive and examines parent components only, not
    the final filename.

    Args:
        path (PurePosixPath): Logical container path to inspect.
        *directory_names (str): Parent directory names to match.

    Returns:
        bool: ``True`` when a parent component matches one of the specified
          directory names.
    """
    expected = {name.casefold() for name in directory_names}
    return any(part.casefold() in expected for part in path.parts[:-1])


def channel_id_from_metadata(payload: JSONValue) -> str | None:
    """Extract a channel ID from channel metadata.

    The root ``channel_id`` or ``id`` field is checked first, followed by
    the same fields in a nested ``channel`` mapping. Field-name matching is
    normalized.

    Args:
        payload (JSONValue): Decoded channel metadata document.

    Returns:
        str | None: Valid Discord channel ID, or ``None`` when unavailable.
    """
    if not isinstance(payload, dict):
        return None

    fields = normalized_mapping(payload)
    for key in ("channelid", "id"):
        channel_id = as_snowflake(fields.get(key))
        if channel_id is not None:
            return channel_id

    channel = fields.get("channel")
    if isinstance(channel, Mapping):
        nested_fields = normalized_mapping(channel)
        for key in ("channelid", "id"):
            channel_id = as_snowflake(nested_fields.get(key))
            if channel_id is not None:
                return channel_id

    return None


def server_id_from_metadata(payload: JSONValue) -> str | None:
    """Extract a server/guild ID from channel metadata.

    Args:
        payload (JSONValue): Decoded channel metadata document.

    Returns:
        str | None: Valid Discord server/guild ID, or ``None`` when unavailable.
    """
    if not isinstance(payload, Mapping):
        return None

    fields = normalized_mapping(payload)
    for key in ("serverid", "guildid"):
        server_id = as_snowflake(fields.get(key))
        if server_id is not None:
            return server_id

    for key in ("server", "guild"):
        nested = fields.get(key)
        if isinstance(nested, Mapping):
            nested_fields = normalized_mapping(nested)
            for nested_key in ("serverid", "guildid", "id"):
                server_id = as_snowflake(nested_fields.get(nested_key))
                if server_id is not None:
                    return server_id

    return None


def channel_id_from_path(path: PurePosixPath) -> str | None:
    """Derive a channel ID from a logical container path.

    Path components are searched from right to left for a Discord snowflake,
    optionally prefixed with ``c`` or ``C``.

    Args:
        path (PurePosixPath): Path whose components may identify a channel.

    Returns:
        str | None: Channel ID without its optional prefix, or ``None``.
    """
    for part in reversed(path.parts):
        match = re.fullmatch(r"[cC]?([0-9]{15,20})", part)
        if match:
            return match.group(1)
    return None


def message_id_from_record(record: Mapping[str, JSONValue]) -> str | None:
    """Extract a message ID from a transcript record.

    The normalized ``message_id`` field is preferred over the normalized
    ``id`` field.

    Args:
        record (Mapping[str, JSONValue]): Transcript record to inspect.

    Returns:
        str | None: Valid Discord message ID, or ``None`` when unavailable.
    """
    fields = normalized_mapping(record)
    for key in ("messageid", "id"):
        message_id = as_snowflake(fields.get(key))
        if message_id is not None:
            return message_id
    return None


def channel_id_from_record(record: Mapping[str, JSONValue]) -> str | None:
    """Extract a channel ID from a transcript record.

    Args:
        record (Mapping[str, JSONValue]): Transcript record to inspect.

    Returns:
        str | None: Valid ``channel_id`` value, or ``None`` when unavailable.
    """
    fields = normalized_mapping(record)
    return as_snowflake(fields.get("channelid"))


def server_id_from_record(record: Mapping[str, JSONValue]) -> str | None:
    """Extract a server/guild ID from a transcript record.

    Args:
        record (Mapping[str, JSONValue]):  Transcript record to inspect.

    Returns:
        str | None: Valid Discord server/guild ID, or ``None`` when unavailable.
    """
    fields = normalized_mapping(record)

    for key in ("serverid", "guildid"):
        server_id = as_snowflake(fields.get(key))
        if server_id is not None:
            return server_id

    for key in ("server", "guild"):
        nested = fields.get(key)
        if isinstance(nested, Mapping):
            nested_fields = normalized_mapping(nested)
            for nested_key in ("serverid", "guildid", "id"):
                server_id = as_snowflake(nested_fields.get(nested_key))
                if server_id is not None:
                    return server_id

    return None


def looks_like_message(record: Mapping[str, JSONValue]) -> bool:
    """Check whether a mapping resembles a Discord message record.

    A matching record contains an identifier field and at least one known
    message-related field after key normalization.

    Args:
        record (Mapping[str, JSONValue]): Mapping to classify.

    Returns:
        bool: ``True`` when the mapping has the expected field combination.
    """
    keys = set(normalized_mapping(record))
    has_identifier = bool(keys & {"id", "messageid"})
    has_message_field = bool(
        keys & {"timestamp", "content", "contents", "attachments", "channelid"},
    )
    return has_identifier and has_message_field


def is_transcript_entry(entry: ContainerEntry) -> bool:
    """Check whether a container entry may contain message records.

    Candidate entries are JSON or CSV files beneath a ``messages``
    directory. Metadata and index files are excluded, and the filename or
    parent path must resemble a Discord transcript location.

    Args:
        entry (ContainerEntry): Container entry to classify.

    Returns:
        bool: ``True`` when the entry is a transcript candidate.
    """
    path = entry.logical_path
    if not is_beneath_named_directory(path, "messages"):
        return False
    if path.suffix.casefold() not in {".json", ".csv"}:
        return False
    if path.stem.casefold() in {"channel", "index"}:
        return False

    likely_name = path.stem.casefold() in {"message", "messages", "transcript"}
    return likely_name or channel_id_from_path(path.parent) is not None


###############################################################################
# Discord container parsing
###############################################################################
JSON_READ_ERRORS = (
    OSError,
    UnicodeError,
    json.JSONDecodeError,
    zipfile.BadZipFile,
    RuntimeError,
)

TRANSCRIPT_READ_ERRORS = (
    *JSON_READ_ERRORS,
    csv.Error,
)


@dataclass(frozen=True, slots=True, order=True)
class MessageReference:
    """The two identifiers needed to locate a Discord message."""

    server_id: str
    channel_id: str
    message_id: str


@dataclass(slots=True)
class ScanReport:
    """Results and diagnostics from scanning message transcript files."""

    references: set[MessageReference] = field(default_factory=set[MessageReference])
    transcript_files: int = 0
    records_seen: int = 0
    skipped_records: int = 0
    malformed_files: int = 0

    @property
    def duplicate_records(self) -> int:
        """The number of duplicate valid references encountered."""
        valid_records = self.records_seen - self.skipped_records
        return max(0, valid_records - len(self.references))

    @property
    def channel_count(self) -> int:
        """The number of distinct channels represented in the scan results."""
        return len({reference.channel_id for reference in self.references})


def load_channel_metadata(
    container: DiscordDataContainer,
) -> dict[PurePosixPath, tuple[str | None, str]]:
    """Load channel IDs from metadata files in the messages tree.

    Unreadable or malformed ``channel.json`` files are logged and skipped.

    Args:
        container (DiscordDataContainer): Open Discord data container.

    Returns:
        dict[PurePosixPath, str]: Channel IDs indexed by transcript directory.
    """
    result: dict[PurePosixPath, tuple[str | None, str]] = {}
    for entry in container.entries:
        if entry.logical_path.name.casefold() != "channel.json":
            continue
        if not is_beneath_named_directory(entry.logical_path, "messages"):
            continue

        try:
            payload = container.load_json(entry)
            channel_id = channel_id_from_metadata(payload)
            server_id = server_id_from_metadata(payload)
        except JSON_READ_ERRORS as exc:
            logger.warning(
                "Unreadable channel metadata; using the path as a fallback: %s",
                entry.logical_path.as_posix(),
            )
            logger.debug("Error details", exc_info=exc)
            continue

        if channel_id is not None:
            result[entry.logical_path.parent] = (server_id, channel_id)

    return result


def message_records_from_json(payload: JSONValue) -> Iterator[Mapping[str, JSONValue]]:
    """Yield message records from supported Discord JSON shapes.

    Supported documents include lists, objects containing ``messages``,
    ``transcript``, or ``data``, single message objects, and objects keyed by
    message ID. For keyed objects, a missing message ID is added to a copy of
    the record when the key is a valid snowflake.

    Args:
        payload (JSONValue): Decoded JSON transcript content.

    Yields:
        Mapping[str, JSONValue]: Message-like mappings in source order.
    """
    if isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, Mapping))
        return

    if not isinstance(payload, Mapping):
        return

    fields = normalized_mapping(payload)
    for container_name in ("messages", "transcript", "data"):
        container = fields.get(container_name)
        if isinstance(container, list):
            yield from (item for item in container if isinstance(item, Mapping))
            return
        if isinstance(container, Mapping):
            for key, item in container.items():
                if not isinstance(item, Mapping):
                    continue
                record = dict(item)
                if message_id_from_record(record) is None:
                    message_id = as_snowflake(key)
                    if message_id is not None:
                        record["ID"] = message_id
                yield record
            return

    if looks_like_message(payload):
        yield payload
        return

    # Some historical exports use an object keyed directly by message ID.
    for key, item in payload.items():
        message_id = as_snowflake(key)
        if message_id is None or not isinstance(item, Mapping):
            continue
        record = dict(item)
        if message_id_from_record(record) is None:
            record["ID"] = message_id
        yield record


def records_from_entry(
    container: DiscordDataContainer,
    entry: ContainerEntry,
) -> Iterator[Mapping[str, JSONValue]]:
    """Yield records from one JSON or CSV transcript entry.

    JSON documents are interpreted through :func:`message_records_from_json`.
    CSV documents are read as dictionaries using their header row.

    Args:
        container (DiscordDataContainer): Open container that owns the entry.
        entry (ContainerEntry): JSON or CSV transcript entry.

    Yields:
        Mapping[str, JSONValue]: Transcript records in source order.

    Raises:
        InvalidContainerError: The entry cannot be opened from the container.
        OSError: The entry cannot be read.
        UnicodeError: The entry is not valid UTF-8 text.
        json.JSONDecodeError: A JSON entry is malformed.
        csv.Error: A CSV entry cannot be parsed.
    """
    if entry.logical_path.suffix.casefold() == ".json":
        yield from message_records_from_json(container.load_json(entry))
        return

    with container.open_text(entry) as stream:
        yield from csv.DictReader(stream)


def scan_message_references(container: DiscordDataContainer) -> ScanReport:
    """Scan a Discord container for unique channel and message IDs.

    Channel IDs are taken from each record when available, then from nearby
    channel metadata, and finally from the transcript path. Malformed files
    and incomplete records are counted and skipped.

    Args:
        container (DiscordDataContainer): Open Discord data container.

    Returns:
        ScanReport: Unique references and transcript diagnostics.

    Raises:
        NoMessagesFoundError: No transcript candidates or valid reference
          pairs are found.
    """
    report = ScanReport()
    channel_metadata = load_channel_metadata(container)
    transcript_entries = [entry for entry in container.entries if is_transcript_entry(entry)]

    if not transcript_entries:
        msg = "No JSON or CSV transcript file was found in the Discord's 'messages' directory."
        raise NoMessagesFoundError(msg)

    for entry in transcript_entries:
        report.transcript_files += 1
        metadata_server_id: str | None = None
        fallback_channel_id: str | None = None

        metadata = channel_metadata.get(entry.logical_path.parent)
        if metadata is not None:
            metadata_server_id, fallback_channel_id = metadata
        if fallback_channel_id is None:
            fallback_channel_id = channel_id_from_path(entry.logical_path.parent)

        try:
            for record in records_from_entry(container, entry):
                report.records_seen += 1
                message_id = message_id_from_record(record)
                channel_id = channel_id_from_record(record) or fallback_channel_id
                server_id = server_id_from_record(record) or metadata_server_id
                if message_id is None or channel_id is None or server_id is None:
                    report.skipped_records += 1
                    continue

                report.references.add(
                    MessageReference(
                        server_id=server_id,
                        channel_id=channel_id,
                        message_id=message_id,
                    ),
                )
        except TRANSCRIPT_READ_ERRORS as exc:
            report.malformed_files += 1
            logger.warning(
                "Transcript skipped because it is unreadable: %s",
                entry.logical_path.as_posix(),
            )
            logger.debug("Error details", exc_info=exc)

    if not report.references:
        msg = "Transcripts were found, but no valid channel_id/message_id pair could be extracted."
        raise NoMessagesFoundError(msg)

    return report


###############################################################################
# Account information discovery
###############################################################################
@dataclass(frozen=True, slots=True)
class AccountInformation:
    """Account fields used to prefill the privacy-request email."""

    username: str | None = None
    user_id: str | None = None
    email: str | None = None


def nested_mappings(payload: JSONValue) -> Iterator[Mapping[str, JSONValue]]:
    """Yield every mapping contained in a decoded JSON value.

    Traversal is depth-first. Each mapping is yielded before mappings nested
    in its values.

    Args:
        payload (JSONValue): JSON value to traverse.

    Yields:
        Mapping[str, JSONValue]: Each mapping in traversal order.
    """
    if isinstance(payload, Mapping):
        yield payload
        for value in payload.values():
            yield from nested_mappings(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from nested_mappings(value)


def account_candidate(record: Mapping[str, JSONValue]) -> tuple[int, AccountInformation]:
    """Score and extract a possible Discord account record.

    The score prioritizes records containing a valid email address, user ID,
    username, and other account-like fields. A nonzero discriminator is
    appended to the username.

    Args:
        record (Mapping[str, JSONValue]): Candidate account mapping.

    Returns:
        tuple[int, AccountInformation]: Candidate score and extracted fields.
    """
    fields = normalized_mapping(record)
    user_id = as_snowflake(fields.get("userid")) or as_snowflake(fields.get("id"))
    username = clean_single_line(fields.get("username"))
    email = clean_single_line(fields.get("email"))
    if email is not None and not EMAIL_PATTERN.fullmatch(email):
        email = None

    discriminator = clean_single_line(fields.get("discriminator"))
    if username and discriminator and discriminator != "0":
        username = f"{username}#{discriminator}"

    score = 0
    score += 4 if email else 0
    score += 3 if user_id else 0
    score += 3 if username else 0
    score += 1 if "phone" in fields else 0
    score += 1 if "settings" in fields else 0

    return score, AccountInformation(username=username, user_id=user_id, email=email)


def discover_account_information(container: DiscordDataContainer) -> AccountInformation:
    """Find the most likely account information in the container.

    JSON files beneath a recognized account directory are searched recursively.
    Unreadable files are skipped, and the first candidate with the highest
    score is retained. Account values are not written to logs.

    Args:
        container (DiscordDataContainer): Open Discord data container.

    Returns:
        AccountInformation: Best discovered fields, or an empty result when
          no candidate scores above zero.
    """
    best_score = 0
    best_information = AccountInformation()

    for entry in container.entries:
        if entry.logical_path.suffix.casefold() != ".json":
            continue
        if not is_beneath_named_directory(entry.logical_path, *ACCOUNT_DIRECTORY_NAMES):
            continue

        try:
            payload = container.load_json(entry)
        except JSON_READ_ERRORS as exc:
            logger.debug(
                "Account file skipped: %s",
                entry.logical_path.as_posix(),
                exc_info=exc,
            )
            continue

        for record in nested_mappings(payload):
            score, information = account_candidate(record)
            if score > best_score:
                best_score = score
                best_information = information

    return best_information


def validate_account_information(discovered: AccountInformation) -> AccountInformation:
    """Valide account fields.

    The user ID and email address are validated before the result is returned.

    Args:
        discovered (AccountInformation): Fields extracted from the container.

    Returns:
        AccountInformation: Validated account fields after applying overrides.

    Raises:
        DiscordPurgerError: An explicit user ID or email address is invalid.
    """
    user_id = discovered.user_id
    if user_id is not None and as_snowflake(user_id) is None:
        raise DiscordPurgerError("The provided user ID is not a valid Discord snowflake.")

    email = discovered.email
    if email is not None:
        email = clean_single_line(email)
        if email is None or not EMAIL_PATTERN.fullmatch(email):
            raise DiscordPurgerError("The provided email address is invalid.")

    username = discovered.username
    return AccountInformation(username=username, user_id=user_id, email=email)


###############################################################################
# Output generation
###############################################################################
def placeholder(label: str) -> str:
    """Create a visible placeholder for a missing email field.

    Args:
        label (str): Description of the value that the user must supply.

    Returns:
        str: Bracketed ``TO COMPLETE`` placeholder.
    """
    return f"[TO COMPLETE: {label}]"


def format_deletion_list(references: set[MessageReference]) -> str:
    """Format message references as a deterministic deletion list.

    References are sorted numerically by server ID, channel ID, and message ID.
    The output starts with a format header and ends with a newline.

    Args:
        references (set[MessageReference]): Unique references to include.

    Returns:
        str: ``server_id:channel_id:message_id`` lines ready for the attachment file.
    """
    ordered = sorted(
        references,
        key=lambda reference: (
            int(reference.server_id),
            int(reference.channel_id),
            int(reference.message_id),
        ),
    )
    return "# server_id:channel_id:message_id\n" + ("").join(
        f"{reference.server_id}:{reference.channel_id}:{reference.message_id}\n"
        for reference in ordered
    )


def format_privacy_email(
    account: AccountInformation,
    report: ScanReport,
    *,
    full_name: str | None,
) -> str:
    """Build a privacy-request email without message contents.

    Missing account or request fields are replaced with visible placeholders.
    The generated text includes the scan totals.

    Args:
        account (AccountInformation): Account fields used to identify the
          request.
        report (ScanReport): Scan totals summarized in the email.
        full_name (str | None): Requester's full name, when available.

    Returns:
        str: Complete plain-text email draft.
    """
    display_name = clean_single_line(full_name) or account.username or placeholder("full name")
    username = account.username or placeholder("Discord username")
    user_id = account.user_id or placeholder("Discord user ID")
    email = account.email or placeholder("email address associated with the account")

    return f"""Recipient: https://support.discord.com/hc/en-us/requests/new
Subject: Data Deletion Request - account {username} ({user_id})

Hello,

I am submitting an erasure request concerning messages sent from my Discord
account, in compliance with GDPR.

Information identifying the account:
- Full name: {display_name}
- Discord username: {username}
- Discord user ID: {user_id}
- Associated email address: {email}

The attached file "{OUTPUT_ATTACHMENT_FILENAME}" lists the messages concerned:
- Unique messages: {len(report.references)}
- Affected channels: {report.channel_count}

Each line uses the "server_id:channel_id:message_id" format. To limit the disclosure of
personal data, this file contains neither the message text nor my attachments.

Please confirm receipt of this request, let me know whether additional identity
verification is required, and confirm when processing is complete. If any
content is refused erasure or retained, please state the reason and applicable
retention period.

Thank you in advance,
{display_name}
"""


def ensure_outputs_available(paths: Sequence[Path], *, force: bool) -> None:
    """Prevent unintended replacement of existing output files.

    Args:
        paths (Sequence[Path]): Destination paths to check.
        force (bool): Whether existing destinations may be replaced.

    Raises:
        OutputFileExistsError: At least one destination exists and ``force``
          is ``False``.
    """
    if force:
        return
    existing = [path for path in paths if path.exists()]
    if existing:
        names = ", ".join(path.name for path in existing)
        msg = f"Output file(s) already exist: {names}. Use --force to replace them."
        raise OutputFileExistsError(msg)


def remove_temporary_file(path: Path) -> None:
    """Remove a temporary file without interrupting cleanup.

    Missing files are ignored. Other removal errors are logged at debug level
    and suppressed.

    Args:
        path (Path): Temporary file to remove.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug(
            "Unable to delete temporary file %s",
            path,
            exc_info=True,
        )


def write_text_files_atomically(files: Mapping[Path, str]) -> None:
    """Write each UTF-8 file through a temporary file.

    Parent directories are created first. Each temporary file is fully
    written before it replaces its destination, then destination permissions
    are restricted to the current user when supported. Remaining temporary
    files are removed during cleanup.

    Args:
        files (Mapping[Path, str]): Text content indexed by destination path.

    Raises:
        OSError: A directory, temporary file, or destination cannot be
          created, written, or replaced.
    """
    temporary_paths: dict[Path, Path] = {}
    try:
        for destination, content in files.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(content)
                temporary_paths[destination] = Path(stream.name)

        for destination, temporary_path in temporary_paths.items():
            temporary_path.replace(destination)
            try:
                destination.chmod(0o600)
            except OSError:
                logger.debug("Unable to restrict permissions for %s", destination)
    finally:
        for temporary_path in temporary_paths.values():
            remove_temporary_file(temporary_path)


###############################################################################
### Command-line interface and main code
###############################################################################
def convert_to_iso_date(value: str) -> str:
    """Parse an ISO calendar date for the command-line interface.

    Args:
        value (str): Date text to validate.

    Returns:
        str: Validated date in canonical ``YYYY-MM-DD`` format.

    Raises:
        argparse.ArgumentTypeError: The value is not a valid ISO date.
    """
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Invalid date; use the YYYY-MM-DD format") from exc


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser.

    Returns:
        argparse.ArgumentParser: Parser configured for package analysis and
          output generation options.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Locally analyzes personal Discord data and generates an identifier "
            "list and a draft erasure request. No network communication is "
            "performed."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-data",
        "--data-source",
        type=Path,
        dest="data_source",
        default=DISCORD_DATA_ARCHIVE_FILE.resolve(),
        metavar="<path>",
        help="Path to the directory or ZIP archive with the Discord data.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        dest="output_dir",
        default=OUTPUT_DIRECTORY.resolve(),
        metavar="<path>",
        help="Path to the directory in which to create the two files.",
    )
    parser.add_argument(
        "--full-name",
        type=str,
        metavar="<firstname> <lastname>",
        help=(
            "Full name to include in the message. "
            "Default to the extracted Discord username if not specified."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace output files if they already exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze the data without writing output files.",
    )
    return parser


def setup_logger() -> None:
    """Configure application logging to a file and standard output.

    Existing root logging handlers are replaced. Messages at INFO level or
    above are written to both destinations, and the log directory is created
    when absent.

    Raises:
        OSError: The log directory or log file cannot be created or opened.
    """
    LOG_DIRECTORY.mkdir(parents=False, exist_ok=True)
    file_handler = logging.FileHandler(
        filename=(LOG_DIRECTORY / LOG_FILENAME).resolve(),
        encoding="utf-8",
        mode="a",
    )
    console_handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)-8s] %(message)s",
        style="%",
        datefmt="%Y-%m-%d %H:%M",
        handlers=[file_handler, console_handler],
        force=True,
    )


def analyze_container(container_path: Path) -> tuple[ScanReport, AccountInformation]:
    """Scan a Discord data container and discover account fields.

    Args:
        container_path (Path): Directory or ZIP archive to analyze.

    Returns:
        tuple[ScanReport, AccountInformation]: Message-reference diagnostics
          and the most likely account information.

    Raises:
        InvalidContainerError: The container path cannot be opened or scanned.
        NoMessagesFoundError: No valid message references can be extracted.
    """
    with DiscordDataContainer(container_path) as container:
        return (
            scan_message_references(container),
            discover_account_information(container),
        )


def run(arguments: argparse.Namespace) -> None:
    """Execute the package analysis and output-generation workflow.

    Command-line account fields override discovered values. In dry-run mode,
    the package is analyzed and summarized without writing files. Otherwise,
    the deletion list and email draft are written to the output directory.

    Args:
        arguments (argparse.Namespace): Parsed command-line arguments.

    Raises:
        DiscordPurgerError: An expected validation, container, scan, or
          output-conflict error occurs.
        OSError: An output directory or file cannot be created or written.
    """
    logger.info("Local Discord data package analysis.")
    data_source_path = arguments.data_source or DISCORD_DATA_ARCHIVE_FILE or DISCORD_DATA_DIRECTORY
    report, discovered_account = analyze_container(data_source_path)

    account = validate_account_information(discovered_account)
    logger.info(
        "%d unique message(s) found in %d channel(s).",
        len(report.references),
        report.channel_count,
    )
    logger.info(
        "%d transcript(s) analyzed, %d duplicate(s), %d record(s) skipped, %d unreadable file(s).",
        report.transcript_files,
        report.duplicate_records,
        report.skipped_records,
        report.malformed_files,
    )

    missing_fields = [
        label
        for label, value in (
            ("username", account.username),
            ("user ID", account.user_id),
            ("email address", account.email),
            ("full name", clean_single_line(arguments.full_name)),
        )
        if not value
    ]
    if missing_fields:
        logger.warning(
            "Fields to review or complete in the request message: %s.",
            ", ".join(missing_fields),
        )

    if arguments.dry_run:
        logger.info("Dry-run mode: no output files were written.")
        return

    output_directory = arguments.output_dir.expanduser().resolve()
    output_paths = (
        output_directory / OUTPUT_ATTACHMENT_FILENAME,
        output_directory / OUTPUT_MESSAGE_FILENAME,
    )
    ensure_outputs_available(output_paths, force=arguments.force)

    output_contents = {
        output_paths[0]: format_deletion_list(report.references),
        output_paths[1]: format_privacy_email(
            account,
            report,
            full_name=arguments.full_name,
        ),
    }
    write_text_files_atomically(output_contents)

    logger.info("File created: %s", output_paths[0])
    logger.info("File created: %s", output_paths[1])
    logger.warning(
        "Before sending, review the bracketed fields in %s, check %s, and never "
        "share your password, Discord token, or MFA code.",
        OUTPUT_MESSAGE_FILENAME,
        OUTPUT_ATTACHMENT_FILENAME,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Discord Purger command-line application.

    Args:
        argv (Sequence[str] | None): Arguments to parse instead of
          :data:`sys.argv`, or ``None`` to use the process arguments.

    Returns:
        int: ``0`` after successful completion, or ``1`` when processing
          raises an error.

    Raises:
        SystemExit: Argument parsing requests help or rejects the arguments.
    """
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)

    try:
        setup_logger()
        logger.info("Starting %s script.", APPLICATION_NAME)
        run(arguments)
    except DiscordPurgerError as exc:
        logger.error("%s", exc)  # noqa: TRY400
        return 1
    except Exception:
        logger.exception("Operation interrupted.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
