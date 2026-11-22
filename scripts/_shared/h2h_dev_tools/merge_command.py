"""Own a merge/review command and its process tree until cleanup completes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

if TYPE_CHECKING:
    from .process import ProcessCommand

REVIEW_OWNER_ENVIRONMENT = (
    "H2H_REVIEW_PROCESS_OWNER",
    "H2H_REVIEW_OWNER_SOURCE",
    "H2H_REVIEW_LOCK_TARGET",
    "H2H_REVIEW_PENDING_RECEIPT",
    "H2H_REVIEW_COMPLETION_RECEIPT",
    "H2H_REVIEW_DEADLINE",
)
RECOVERY_TIMEOUT_SECONDS = 10.0
RECOVERY_DIAGNOSTIC_LIMIT_BYTES = 64 * 1024
OWNERSHIP_FAILURE_LIMIT_BYTES = 64 * 1024
REVIEW_COMPLETION_LIMIT_BYTES = 4096


def _ownership_failure_payload(error: BaseException, *, boundary: str) -> bytes:
    """Describe retained exceptions without source, locals, or owner inspection."""
    truncated = False

    def text(value: str, limit: int) -> str:
        nonlocal truncated
        # Count escaped JSON bytes, including quotes; slicing first also bounds
        # encoding work for an unusually large message or note.
        low, high = 0, min(len(value), limit)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(value[:middle], ensure_ascii=True)) <= limit:
                low = middle
            else:
                high = middle - 1
        truncated |= low < len(value)
        return value[:low]

    pending = [error]
    identities = {id(error): 0}

    def reference(related: BaseException | None) -> int | str | None:
        nonlocal truncated
        if related is None:
            return None
        if id(related) in identities:
            return identities[id(related)]
        if len(pending) == 8:
            truncated = True
            return "omitted"
        identities[id(related)] = len(pending)
        pending.append(related)
        return identities[id(related)]

    exceptions: list[dict[str, object]] = []
    for current in pending:
        notes = getattr(current, "__notes__", ())
        if not isinstance(notes, (list, tuple)):
            notes = ()
            truncated = True
        truncated |= len(notes) > 8
        exceptions.append(
            {
                "type": text(
                    f"{type(current).__module__}.{type(current).__qualname__}", 256
                ),
                "message": text(str(current), 2048),
                "notes": [text(note, 512) for note in notes[:8]],
                "cause": reference(current.__cause__),
                "context": reference(
                    None if current.__suppress_context__ else current.__context__
                ),
                "context_suppressed": current.__suppress_context__,
            }
        )
    document = {
        "schema": 1,
        "kind": "merge-owner-failure-diagnostic",
        "authoritative": False,
        "exit_code": 125,
        "cleanup_state": "unknown",
        "boundary": text(boundary, 128),
        "exceptions": exceptions,
        "truncated": truncated,
    }
    payload = (
        json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    if len(payload) > OWNERSHIP_FAILURE_LIMIT_BYTES:
        raise ValueError("Ownership failure diagnostic exceeds its byte limit")
    return payload


def _record_ownership_failure(
    receipt: Path, error: BaseException, *, boundary: str
) -> None:
    """Best-effort terminal metadata, never a cleanup receipt or a new writer.

    A single write may fail or be partial; readers must reject invalid JSON.
    No retry, fsync, new deadline or process is admitted. Like other filesystem
    metadata operations, this does not guarantee syscall completion by a deadline.
    """
    try:
        payload = _ownership_failure_payload(error, boundary=boundary)
        destination = receipt.with_name(receipt.name + ".failure.json")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except Exception:
        # Optional diagnostics cannot replace the original ownership failure or
        # admit fallback output after cleanup has become unknown.
        pass


def without_review_owner(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environment.items()
        if name not in REVIEW_OWNER_ENVIRONMENT
    }


def owner_source_files() -> tuple[str, ...]:
    from . import bundle_verify

    # The review owner now validates and publishes review evidence itself. Use
    # the authoritative review closure rather than a second dependency list.
    return tuple(sorted(bundle_verify.component_files(["code-review"])))


def owner_source_digest() -> str:
    """Bind the complete copied owner implementation used by managed review."""
    digest = hashlib.sha256()
    for name in owner_source_files():
        source = Path(__file__).with_name(name).read_bytes()
        digest.update(name.encode("ascii") + b"\0" + hashlib.sha256(source).digest())
    return digest.hexdigest()


def write_review_completion(
    *, target_key: str, status: int, completion: Literal["not-started", "attested"]
) -> None:
    """Record a normally completed managed reviewer, never a process census.

    The authenticated child calls this after all its managed work and diagnostics
    finish. The parent still owns and reaps that child before accepting the record.
    An exclusive file in the parent's fresh directory prevents reuse; partial I/O
    or an interrupted write is not a completed handshake.
    """
    if type(status) is not int or status not in (0, 1):
        raise ValueError("Managed review completion requires normal status 0 or 1")
    if completion not in ("not-started", "attested") or (
        status == 0 and completion != "attested"
    ):
        raise ValueError("Managed review completion requires completed tool work")
    document = {
        "schema": 1,
        "kind": "managed-review-completion",
        "target_key": target_key,
        "owner_pid": int(os.environ["H2H_REVIEW_PROCESS_OWNER"]),
        "owner_source": os.environ["H2H_REVIEW_OWNER_SOURCE"],
        "exit_status": status,
        "completion": completion,
    }
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) > REVIEW_COMPLETION_LIMIT_BYTES:
        raise ValueError("Managed review completion exceeds its byte limit")
    destination = Path(os.environ["H2H_REVIEW_COMPLETION_RECEIPT"])
    with destination.open("xb") as stream:
        stream.write(payload)


def _require_review_completion(
    path: Path, *, target_key: str, status: int, owner_source: str
) -> None:
    from . import review_coverage, review_execution, review_plan

    try:
        with path.open("rb") as stream:
            document = review_coverage.strict_json(
                stream.read(REVIEW_COMPLETION_LIMIT_BYTES + 1),
                maximum=REVIEW_COMPLETION_LIMIT_BYTES,
            )
        if not isinstance(document, dict):
            raise ValueError("Managed review completion must be a JSON object")
        expected = {
            "schema": 1,
            "kind": "managed-review-completion",
            "target_key": target_key,
            "owner_pid": os.getpid(),
            "owner_source": owner_source,
            "exit_status": status,
            "completion": document.get("completion"),
        }
        if (
            type(status) is not int
            or status not in (0, 1)
            or document != expected
            or type(document.get("schema")) is not int
            or type(document.get("owner_pid")) is not int
            or type(document.get("exit_status")) is not int
            or document.get("completion") not in ("not-started", "attested")
            or (status == 0 and document.get("completion") != "attested")
        ):
            raise ValueError("Managed review completion identity or status differs")
    except (OSError, ValueError, review_plan.PlanError) as error:
        raise review_execution.ReviewCompletionUnproven(
            "Managed reviewer has no matching normal-completion record; "
            "retain candidate, task and workflow admission"
        ) from error


def _run_owned(phase: ProcessCommand, *, hard_deadline: float | None = None) -> int:
    from . import process

    try:
        return process.run_command(phase, hard_deadline=hard_deadline)
    except KeyboardInterrupt:
        return process.INTERRUPTED_EXIT_CODE
    except process.RunnerSignalInterrupt as error:
        return 128 + error.signal_number
    except process.ProcessOwnershipError as error:
        if not error.cleanup_proven:
            raise
        process.report_diagnostic(
            f"merge-command: surviving descendants required cleanup: {error}",
            deadline=hard_deadline,
        )
        return 1


def _replay_recovery_output(output: BinaryIO, *, deadline: float) -> None:
    import codecs

    from . import process

    try:
        output.seek(0)
        remaining = RECOVERY_DIAGNOSTIC_LIMIT_BYTES
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while remaining:
            if time.monotonic() >= deadline:
                return
            chunk = output.read(min(2048, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            process.report_diagnostic(
                decoder.decode(chunk), deadline=deadline, newline=False
            )
        tail = decoder.decode(b"", final=True)
        if tail:
            process.report_diagnostic(tail, deadline=deadline, newline=False)
        if remaining == 0 and output.read(1):
            process.report_diagnostic(
                "\nmerge-command: recovery diagnostics truncated after 64 KiB",
                deadline=deadline,
            )
    except OSError as error:
        process.report_diagnostic(
            f"merge-command: could not read recovery diagnostics: {error}",
            deadline=deadline,
        )


def _run_git(
    phase: ProcessCommand, *, recovery: bool, deadline: float | None = None
) -> int:
    from . import process

    if not recovery:
        return _run_owned(phase)
    if deadline is None:
        raise ValueError("Recovery requires its existing absolute deadline")
    # Git resets SIGPIPE. Capture only recovery's human output until its owned
    # command has exited; replay shares the original ten-second phase deadline.
    output = tempfile.TemporaryFile()
    diagnostics_clean = False
    try:
        status = _run_owned(
            replace(phase, stdout=output, stderr=output), hard_deadline=deadline
        )
        _replay_recovery_output(output, deadline=deadline)
        diagnostics_clean = True
        return status
    finally:
        try:
            output.close()
        except OSError as error:
            # Do not replace an ownership failure or spawn another writer after
            # an uncertain replay. A normal file-close error is optional output.
            if diagnostics_clean:
                process.report_diagnostic(
                    f"merge-command: could not close recovery diagnostics: {error}",
                    deadline=deadline,
                )


@contextmanager
def _review(
    repository: Path, command: tuple[str, ...], environment: dict[str, str]
) -> Iterator[int]:
    from . import process

    # Classify under ownership before requiring any online-review prerequisites.
    # Only an empty, successful classifier tree admits the next phase.
    with tempfile.TemporaryFile() as output:
        status = _run_owned(
            process.ProcessCommand(
                "Classify review candidate",
                (
                    sys.executable,
                    "scripts/check_change_scope.py",
                    "--index",
                    "--base",
                    "HEAD",
                ),
                repository,
                environment=environment,
                stdout=output,
            )
        )
        if status != 0:
            # No model invocation has been admitted. A classifier's ordinary
            # exit 125 is not an ownership failure after _run_owned proved cleanup.
            yield 1 if status == process.TERMINATION_FAILED_EXIT_CODE else status
            return
        output.seek(0)
        scope = output.read().decode("utf-8").strip()
    if scope == "documentation":
        process.report_diagnostic(
            "Documentation-only candidate: online code review is not required.",
            standard_output=True,
        )
        yield 0
        return
    if scope != "full":
        process.report_diagnostic(f"merge-command: invalid check scope: {scope}")
        yield 1
        return
    if os.name != "posix":
        process.report_diagnostic(
            "merge-command: online code review currently requires POSIX"
        )
        yield 1
        return

    from . import code_review, review_execution

    try:
        timeout = int(environment.get("H2H_CODE_REVIEW_TIMEOUT_SECONDS", "900"))
        if not 1 <= timeout <= review_execution.MAX_REVIEW_SECONDS:
            raise ValueError(
                "Review timeout must be between 1 and "
                f"{review_execution.MAX_REVIEW_SECONDS} seconds"
            )
        minimum = int(environment.get("H2H_CODE_REVIEW_MIN_SHARD_SECONDS", "0"))
        if not 0 <= minimum <= review_execution.MAX_REVIEW_SECONDS:
            raise ValueError(
                "Review minimum shard estimate must be between 0 and "
                f"{review_execution.MAX_REVIEW_SECONDS} seconds"
            )
    except ValueError as error:
        process.report_diagnostic(f"merge-command: {error}")
        yield 1
        return
    deadline = time.monotonic() + timeout
    environment["H2H_REVIEW_DEADLINE"] = str(deadline)
    reviewer = code_review.Reviewer(repository)
    with review_execution.within_deadline(deadline):
        target = reviewer._target(index=True)
        reviewer._assert_workspace(target)
        destination = reviewer._receipt_path(target)
        directory = reviewer._directory()
    with reviewer._review_lock(target, exclusive=True):
        # A lost reviewer can leave tool processes in independent groups. Keep
        # this durable admission marker even if our directly managed group exits.
        marker: bytes | None = None
        pending_directory: Path | None = None
        try:
            with code_review._publication(destination) as publication:
                with review_execution.within_deadline(deadline):
                    # Retain the pending files on unknown completion, both for
                    # diagnosis and to avoid racing a surviving reader/writer.
                    pending_directory = Path(
                        tempfile.mkdtemp(prefix="pending-", dir=directory)
                    )
                    pending = pending_directory / "receipt.json"
                    completion = pending_directory / "completion.json"
                    owner_source = owner_source_digest()
                    environment.update(
                        H2H_REVIEW_PROCESS_OWNER=str(os.getpid()),
                        H2H_REVIEW_OWNER_SOURCE=owner_source,
                        H2H_REVIEW_LOCK_TARGET=target.key,
                        H2H_REVIEW_PENDING_RECEIPT=str(pending),
                        H2H_REVIEW_COMPLETION_RECEIPT=str(completion),
                        H2H_REVIEW_DEADLINE=str(deadline),
                    )
                    marker = reviewer._mark_review_pending(target)
                    # Preserve actual signal/ownership failures: the generic Git
                    # helper deliberately turns cleaned interruptions into status
                    # codes, but a review's detached tools need explicit completion.
                    status = process.run_command(
                        process.ProcessCommand(
                            "Code review", command, repository, environment=environment
                        ),
                        hard_deadline=deadline,
                    )
                    _require_review_completion(
                        completion,
                        target_key=target.key,
                        status=status,
                        owner_source=owner_source,
                    )
                    if status == 0:
                        process.report_diagnostic(
                            "Code review managed cleanup and normal completion "
                            f"verified; publishing {reviewer._receipt_path(target)}",
                            standard_output=True,
                            deadline=deadline,
                        )
                        reviewer._publish_managed_receipt(
                            target, pending, destination=publication.staging
                        )
                    # The caller writes its owned-tree proof while publication
                    # still owns rollback; a failed proof cannot authorize merge.
                    yield status
                    shutil.rmtree(pending_directory)
                    pending_directory = None
                    # Finish every diagnostic/process and preparation cleanup
                    # before releasing the durable barrier under the flock. Only
                    # deadline/signal checks and terminal publication remain.
                    reviewer._clear_review_pending(target, marker)
                    marker = None
                if status == 0:
                    publication.complete(deadline)
        except BaseException as error:
            if marker is None:
                # The reviewer was never admitted, or its normal completion
                # was already confirmed under the still-held candidate flock.
                if pending_directory is not None:
                    shutil.rmtree(pending_directory)
                raise
            if isinstance(error, review_execution.ReviewCompletionUnproven):
                raise
            raise review_execution.ReviewCompletionUnproven(
                "Managed review did not finish its normal completion protocol; "
                "candidate, task and admission require cleanup confirmation"
            ) from error


def _execute(
    repository: Path,
    command: tuple[str, ...],
    *,
    review: bool,
    recovery: bool,
    receipt: Path,
    release_workflow: Path | None = None,
) -> int:
    from . import code_review, process, review_execution, review_plan

    environment = without_review_owner(os.environ)
    recovery_deadline = (
        time.monotonic() + RECOVERY_TIMEOUT_SECONDS if recovery else None
    )

    def report(message: str) -> None:
        # A proof written inside publication covers only work already cleaned.
        # Revoke it before admitting another diagnostic writer; failed unlink
        # must stop here, and unknown writer cleanup must leave no stale proof.
        receipt.unlink(missing_ok=True)
        review_deadline = environment.get("H2H_REVIEW_DEADLINE")
        process.report_diagnostic(
            message,
            deadline=float(review_deadline)
            if review_deadline is not None
            else recovery_deadline,
        )

    try:
        if review:
            with _review(repository, command, environment) as status:
                if status == process.TERMINATION_FAILED_EXIT_CODE:
                    raise review_execution.ReviewCompletionUnproven(
                        "Review reported unproven completion"
                    )
                _write_cleanup_receipt(receipt)
        else:
            status = _run_git(
                process.ProcessCommand(
                    "Git workflow and hooks",
                    command,
                    repository,
                    environment=environment,
                ),
                recovery=recovery,
                deadline=recovery_deadline,
            )
            _write_cleanup_receipt(receipt)
    except process.ProcessOwnershipError as error:
        # Publication/terminal checks can fail after the provisional phase proof.
        # Exit 125 still retains admission even if revoking that file itself fails.
        try:
            receipt.unlink(missing_ok=True)
        except OSError as revoke_error:
            error.add_note(f"Could not revoke phase cleanup receipt: {revoke_error}")
        _record_ownership_failure(
            receipt,
            error,
            boundary="execute.review"
            if review
            else "execute.recovery"
            if recovery
            else "execute.git",
        )
        return process.TERMINATION_FAILED_EXIT_CODE
    except (KeyboardInterrupt, process.RunnerSignalInterrupt) as error:
        if not review:
            raise
        # Publication owns signal rollback after all nested commands have been
        # cleaned. A failed command cleanup raises ProcessOwnershipError above,
        # so interruption here can report an empty tree. Pre-commit cancellation
        # revokes review evidence; post-commit cancellation may retain it but
        # still blocks this merge. Preserve the original signal exit status.
        report("merge-command: review publication interrupted")
        status = (
            process.INTERRUPTED_EXIT_CODE
            if isinstance(error, KeyboardInterrupt)
            else 128 + error.signal_number
        )
        _write_cleanup_receipt(receipt)
    except (
        code_review.ReviewError,
        review_execution.ExecutionError,
        review_plan.PlanError,
    ) as error:
        # Every nested owner either completed or propagated ownership failure
        # before this evidence/deadline error; preserve failed status and cleanup.
        report(f"merge-command: {error}")
        status = 1
        _write_cleanup_receipt(receipt)
    if release_workflow is not None and status == 0:
        from . import merge_admission

        # The receipt proves process cleanup only, never successful admission
        # release. Normal completion retains the outer lifetime; failure recovery
        # must still finish within its original phase deadline. Require exit zero.
        if recovery_deadline is not None and time.monotonic() >= recovery_deadline:
            return 1
        try:
            merge_admission.release(release_workflow)
        except merge_admission.AdmissionError, OSError, ValueError, TypeError:
            # Some terminal unlinks may already have completed. No diagnostic
            # writer or subprocess may start after this point, even on failure.
            return 1
    # Reserve 125 for an unproven owner cleanup, never for a hook's own exit code.
    return 1 if status == process.TERMINATION_FAILED_EXIT_CODE else status


def _write_cleanup_receipt(receipt: Path) -> None:
    # An exit status alone cannot distinguish a cleaned interrupt from an owner
    # killed before/during teardown. Write only after every started phase is empty.
    temporary = receipt.with_suffix(".pending")
    temporary.write_text("owned_tree_empty\n", encoding="ascii")
    temporary.replace(receipt)


def _workflow_diagnostic(
    message: str, receipt: Path, *, completion: bool = False
) -> int:
    from . import process

    # Invoked with -I -S -B: no site code can launch arbitrary descendants before
    # ownership begins. Only report_diagnostic admits a fixed, bounded writer;
    # the shell waits/reaps this launcher and requires proof for that new child.
    try:
        with (
            os.fdopen(3, "w", encoding="utf-8", closefd=False) as diagnostic,
            redirect_stderr(diagnostic),
        ):
            process.report_diagnostic(
                message if completion else f"git-flow-merge: {message}"
            )
    except process.ProcessOwnershipError as error:
        _record_ownership_failure(
            receipt,
            error,
            boundary="workflow.completion" if completion else "workflow.diagnostic",
        )
        return process.TERMINATION_FAILED_EXIT_CODE
    _write_cleanup_receipt(receipt)
    return 0


def main() -> int:
    from . import process

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-receipt", required=True, type=Path)
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--release-workflow", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--recovery",
        action="store_true",
        help="bound this failure-recovery command and its cleanup to 10 seconds",
    )
    parser.add_argument(
        "--recovery-output-fds", nargs=2, type=int, help=argparse.SUPPRESS
    )
    parser.add_argument("repository", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a Git workflow command is required")
    if arguments.review and arguments.recovery:
        parser.error("review cannot run as failure recovery")
    if arguments.recovery_output_fds and not arguments.recovery:
        parser.error("recovery output descriptors require --recovery")
    if arguments.release_workflow is not None and arguments.review:
        parser.error("workflow release cannot run as review")
    with ExitStack() as diagnostics:
        if arguments.recovery_output_fds:
            # Startup errors go to /dev/null. Only the common diagnostic writer
            # receives the original destinations, through ordinary text streams.
            for descriptor, redirect in zip(
                arguments.recovery_output_fds,
                (redirect_stdout, redirect_stderr),
                strict=True,
            ):
                stream = diagnostics.enter_context(
                    os.fdopen(descriptor, "w", encoding="utf-8", closefd=False)
                )
                diagnostics.enter_context(redirect(stream))
        try:
            return _execute(
                arguments.repository,
                tuple(command),
                review=arguments.review,
                recovery=arguments.recovery,
                receipt=arguments.cleanup_receipt,
                release_workflow=arguments.release_workflow,
            )
        except process.ProcessOwnershipError as error:
            _record_ownership_failure(arguments.cleanup_receipt, error, boundary="main")
            return process.TERMINATION_FAILED_EXIT_CODE
        except OSError:
            # Receipt/publication I/O can fail after the owned command and its
            # deadline have ended. Never emit an unbounded traceback to the
            # caller's pipe or admit a new reporter after this cleanup boundary.
            return 1


if __name__ == "__main__":
    # The shell copies the source closure before switching to primary. Give its
    # directly executed entry the same relative imports as packaged/snapshot use.
    package = Path(__file__).resolve().parent
    runpy.run_path(str(package / "source_loader.py"))["load_package"](
        package, "h2h_dev_tools"
    )
    __package__ = "h2h_dev_tools"
    if sys.argv[1:] == ["--print-source-files"]:
        print("\n".join(owner_source_files()))
        raise SystemExit(0)
    if sys.argv[1:2] == ["--workflow-state"]:
        from .merge_admission import main as workflow_state_main

        raise SystemExit(workflow_state_main(sys.argv[2:]))
    if len(sys.argv) == 4 and sys.argv[1] in {
        "--workflow-diagnostic",
        "--workflow-completion",
    }:
        # FD 3 is the original output destination; startup errors remain null.
        raise SystemExit(
            _workflow_diagnostic(
                sys.argv[3],
                Path(sys.argv[2]),
                completion=sys.argv[1] == "--workflow-completion",
            )
        )
    raise SystemExit(main())
