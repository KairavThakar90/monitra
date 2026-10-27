import React, { useMemo, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Card, EmptyState, ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import {
  FieldError,
  FormErrorBanner,
  type ValidationResult,
  validateDescription,
  validateInteger,
  validatePlainText,
  validateSha256,
  validateUrl,
  validateVersion,
} from "../../validation";
import {
  AUDIT_ACTION_LABELS,
  PLATFORM_LABELS,
  STATUS_LABELS,
  useCreateReleaseVersionMutation,
  useGetReleaseVersionQuery,
  useGetReleaseVersionsQuery,
  usePublishReleaseVersionMutation,
  useUpdateReleaseVersionMutation,
  useValidateReleaseVersionMutation,
  useWithdrawReleaseVersionMutation,
  type ReleaseArtifactInput,
  type ReleasePlatform,
  type ReleaseVersion,
  type ReleaseVersionDetail,
} from "../../store/api/desktopReleasesApi";

/**
 * Admin Settings -> Desktop Releases.
 *
 * Where an administrator decides what the desktop fleet is offered: create a
 * version (title, notes, platforms, mandatory or optional, the grace period,
 * the floor), check it is complete, publish it, and withdraw it. Publishing is
 * what reaches users -- a draft affects nobody -- and a mandatory release
 * starts each user's grace period the first time their desktop sees it, on
 * the server's clock. The page shows what happened afterwards: who was
 * targeted, how many grace periods are running or over, what the desktops
 * reported about installing it, how far adoption has got, and the
 * announcement's outbox status. Every action is refused server-side for anyone
 * but an administrator, whatever this page renders.
 */

const TITLE_MAX_LENGTH = 120;
const GRACE_MIN_HOURS = 0;
const GRACE_MAX_HOURS = 720;
const DEFAULT_GRACE_HOURS = 24;

/** The value of a validation result that is known to be ok, else the fallback. */
const valueOf = <T,>(result: ValidationResult<T>, fallback: T): T => (result.ok ? result.value : fallback);

const inputClass =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";
const labelClass = "mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500";
const buttonClass =
  "rounded-lg px-3 py-1.5 text-xs font-bold transition disabled:cursor-not-allowed disabled:opacity-50";

const formatWhen = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

const platformWords = (platforms: ReleasePlatform[]) =>
  platforms.map((p) => PLATFORM_LABELS[p] ?? p).join(" + ") || "none";

const policyWords = (release: ReleaseVersion) => {
  if (release.mandatory) {
    return `Mandatory · ${release.grace_hours} h grace · floor ${release.enforced_floor ?? release.version}`;
  }
  return release.enforced_floor ? `Optional · required below ${release.enforced_floor}` : "Optional";
};

const STATUS_STYLES: Record<string, string> = {
  draft: "bg-slate-100 text-slate-600",
  validated: "bg-sky-50 text-sky-700",
  published: "bg-emerald-50 text-emerald-700",
  withdrawn: "bg-rose-50 text-rose-700",
  superseded: "bg-amber-50 text-amber-700",
};

const StatusPill: React.FC<{ status: string }> = ({ status }) => (
  <span className={`inline-block rounded-full px-2.5 py-0.5 text-[11px] font-bold ${STATUS_STYLES[status] ?? "bg-slate-100 text-slate-600"}`}>
    {STATUS_LABELS[status as keyof typeof STATUS_LABELS] ?? status}
  </span>
);

/** The artifacts a target set needs, in the order the pipeline builds them. */
const ARTIFACT_SLOTS: { key: string; platform: ReleasePlatform; architecture: string | null; label: string; hint: string }[] = [
  { key: "win32", platform: "win32", architecture: null, label: "Windows installer", hint: "Monitra-Setup-<version>.exe" },
  { key: "darwin-arm64", platform: "darwin", architecture: "arm64", label: "macOS Apple Silicon", hint: "Monitra-macOS-arm64-<version>.dmg" },
  { key: "darwin-x86_64", platform: "darwin", architecture: "x86_64", label: "macOS Intel", hint: "Monitra-macOS-x86_64-<version>.dmg" },
];

type ArtifactDraft = { download_url: string; sha256: string; file_size: string; signed: boolean };

const emptyArtifact = (): ArtifactDraft => ({ download_url: "", sha256: "", file_size: "", signed: false });

type Editing = { kind: "new" } | { kind: "edit"; release: ReleaseVersion };

const ReleaseDialog: React.FC<{ editing: Editing; onClose: () => void }> = ({ editing, onClose }) => {
  const { showToast } = useFeedback();
  const existing = editing.kind === "edit" ? editing.release : null;
  const published = existing?.status === "published" || existing?.status === "superseded";
  const [version, setVersion] = useState(existing?.version ?? "");
  const [title, setTitle] = useState(existing?.title ?? "");
  const [notes, setNotes] = useState(existing?.release_notes ?? "");
  const [platforms, setPlatforms] = useState<ReleasePlatform[]>(existing?.target_platforms ?? ["win32", "darwin"]);
  const [mandatory, setMandatory] = useState(existing?.mandatory ?? false);
  const [graceHours, setGraceHours] = useState(String(existing?.grace_hours ?? DEFAULT_GRACE_HOURS));
  const [floor, setFloor] = useState(existing?.required_min_version ?? "");
  const [effectiveAt, setEffectiveAt] = useState(existing?.effective_at ? existing.effective_at.slice(0, 16) : "");
  const [withArtifacts, setWithArtifacts] = useState(false);
  const [artifacts, setArtifacts] = useState<Record<string, ArtifactDraft>>(
    () => Object.fromEntries(ARTIFACT_SLOTS.map((slot) => [slot.key, emptyArtifact()])),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [create, { isLoading: creating }] = useCreateReleaseVersionMutation();
  const [update, { isLoading: updating }] = useUpdateReleaseVersionMutation();

  const slots = ARTIFACT_SLOTS.filter((slot) => platforms.includes(slot.platform));

  const togglePlatform = (platform: ReleasePlatform) =>
    setPlatforms((current) =>
      current.includes(platform) ? current.filter((p) => p !== platform) : [...current, platform],
    );

  const setArtifact = (key: string, patch: Partial<ArtifactDraft>) =>
    setArtifacts((current) => ({ ...current, [key]: { ...current[key], ...patch } }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const next: Record<string, string> = {};
    const checkedVersion = validateVersion(version, { fieldLabel: "Version" });
    if (!existing && !checkedVersion.ok) next.version = checkedVersion.error;
    const checkedTitle = validatePlainText(title, { fieldLabel: "Title", maxLength: TITLE_MAX_LENGTH, required: false });
    if (!checkedTitle.ok) next.title = checkedTitle.error;
    const checkedNotes = validateDescription(notes, { fieldLabel: "Release notes", required: false });
    if (!checkedNotes.ok) next.notes = checkedNotes.error;
    if (platforms.length === 0) next.platforms = "Choose at least one platform.";
    const checkedGrace = validateInteger(graceHours, { fieldLabel: "Grace period", minimum: GRACE_MIN_HOURS, maximum: GRACE_MAX_HOURS });
    if (!checkedGrace.ok) next.grace = checkedGrace.error;
    const checkedFloor = validateVersion(floor, { fieldLabel: "Required minimum version", required: false });
    if (!checkedFloor.ok) next.floor = checkedFloor.error;
    const artifactInputs: ReleaseArtifactInput[] = [];
    if (!existing && withArtifacts) {
      for (const slot of slots) {
        const draft = artifacts[slot.key];
        const url = validateUrl(draft.download_url, { fieldLabel: `${slot.label} URL`, required: true });
        if (!url.ok) next[`artifact.${slot.key}.url`] = url.error;
        const digest = validateSha256(draft.sha256, { fieldLabel: `${slot.label} SHA-256` });
        if (!digest.ok) next[`artifact.${slot.key}.sha256`] = digest.error;
        const size = validateInteger(draft.file_size, { fieldLabel: `${slot.label} size`, minimum: 1 });
        if (!size.ok) next[`artifact.${slot.key}.size`] = size.error;
        if (url.ok && digest.ok && size.ok) {
          artifactInputs.push({
            platform: slot.platform, architecture: slot.architecture,
            download_url: url.value, sha256: digest.value,
            file_size: size.value as number, signed: draft.signed,
          });
        }
      }
    }
    setErrors(next);
    if (Object.keys(next).length > 0) {
      setFormError("Please correct the highlighted fields.");
      return;
    }
    setFormError(null);
    // Every result is `ok` past this point (the first failure returned above);
    // read them through `valueOf` so the compiler knows it too.
    const titleValue = valueOf(checkedTitle, "");
    const notesValue = valueOf(checkedNotes, "");
    const graceValue = valueOf(checkedGrace, DEFAULT_GRACE_HOURS) as number;
    const floorValue = valueOf(checkedFloor, "");
    try {
      if (existing) {
        await update({
          version: existing.version,
          body: {
            title: titleValue || null,
            release_notes: notesValue || null,
            ...(published ? {} : { target_platforms: platforms }),
            mandatory,
            grace_hours: graceValue,
            ...(floorValue ? { required_min_version: floorValue } : { clear_required_min_version: true }),
            ...(effectiveAt ? { effective_at: new Date(effectiveAt).toISOString() } : { clear_effective_at: true }),
          },
        }).unwrap();
        showToast("Release updated.", "success");
      } else {
        await create({
          version: valueOf(checkedVersion, version),
          title: titleValue || null,
          release_notes: notesValue || null,
          target_platforms: platforms,
          mandatory,
          grace_hours: graceValue,
          required_min_version: floorValue || null,
          effective_at: effectiveAt ? new Date(effectiveAt).toISOString() : null,
          artifacts: artifactInputs,
        }).unwrap();
        showToast("Release created as a draft.", "success");
      }
      onClose();
    } catch (exc) {
      setFormError(formatApiError((exc as { data?: unknown })?.data, "Could not save the release."));
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <form
        onSubmit={submit}
        noValidate
        role="dialog"
        aria-modal="true"
        aria-label={existing ? "Edit release" : "New release"}
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-slate-200 bg-white shadow-2xl"
      >
        <div className="border-b border-slate-100 bg-slate-50 px-6 py-5">
          <h2 className="text-xl font-black text-slate-800">{existing ? `Edit Monitra ${existing.version}` : "New release"}</h2>
          <p className="mt-1 text-sm font-semibold text-slate-500">
            A release is created as a draft and affects nobody until it is published. A mandatory
            release gives each user a grace period, counted from the first time their desktop sees it,
            before Monitra stops working on the old version.
          </p>
        </div>

        <div className="space-y-5 p-6">
          <FormErrorBanner message={formError} />

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="release-version" className={labelClass}>Version</label>
              <input
                id="release-version" value={version} disabled={Boolean(existing)}
                onChange={(event) => setVersion(event.target.value)} placeholder="1.4.0"
                aria-invalid={errors.version ? true : undefined} className={inputClass}
              />
              <FieldError message={errors.version} />
            </div>
            <div>
              <label htmlFor="release-title" className={labelClass}>Title</label>
              <input
                id="release-title" value={title} maxLength={TITLE_MAX_LENGTH}
                onChange={(event) => setTitle(event.target.value)} placeholder="e.g. Autumn release"
                aria-invalid={errors.title ? true : undefined} className={inputClass}
              />
              <FieldError message={errors.title} />
            </div>
          </div>

          <div>
            <label htmlFor="release-notes" className={labelClass}>Release notes</label>
            <textarea
              id="release-notes" value={notes} rows={4}
              onChange={(event) => setNotes(event.target.value)}
              placeholder={"New features:\n- ...\nBug fixes:\n- ..."}
              aria-invalid={errors.notes ? true : undefined} className={inputClass}
            />
            <FieldError message={errors.notes} />
          </div>

          <div>
            <span className={labelClass}>Platforms</span>
            <div role="group" aria-label="Platforms" className="flex gap-4">
              {(["win32", "darwin"] as ReleasePlatform[]).map((platform) => (
                <label key={platform} className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                  <input
                    type="checkbox" checked={platforms.includes(platform)} disabled={published}
                    onChange={() => togglePlatform(platform)}
                  />
                  {PLATFORM_LABELS[platform]}
                </label>
              ))}
            </div>
            {published && (
              <p className="mt-1 text-xs text-slate-500">The platforms of a published release cannot be changed; withdraw it first.</p>
            )}
            <FieldError message={errors.platforms} />
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <span className={labelClass}>Type</span>
              <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <input type="checkbox" checked={mandatory} onChange={(event) => setMandatory(event.target.checked)} />
                Mandatory
              </label>
              <p className="mt-1 text-xs text-slate-500">
                {mandatory ? "Users below the floor must install it after the grace period." : "Blocks nobody."}
              </p>
            </div>
            <div>
              <label htmlFor="release-grace" className={labelClass}>Grace period (hours)</label>
              <input
                id="release-grace" value={graceHours} inputMode="numeric"
                onChange={(event) => setGraceHours(event.target.value)}
                aria-invalid={errors.grace ? true : undefined} className={inputClass}
              />
              <FieldError message={errors.grace} />
            </div>
            <div>
              <label htmlFor="release-floor" className={labelClass}>Required minimum version</label>
              <input
                id="release-floor" value={floor} onChange={(event) => setFloor(event.target.value)}
                placeholder={mandatory ? "defaults to this version" : "optional"}
                aria-invalid={errors.floor ? true : undefined} className={inputClass}
              />
              <FieldError message={errors.floor} />
            </div>
          </div>

          <div>
            <label htmlFor="release-effective" className={labelClass}>Effective from (optional)</label>
            <input
              id="release-effective" type="datetime-local" value={effectiveAt}
              onChange={(event) => setEffectiveAt(event.target.value)} className={inputClass}
            />
            <p className="mt-1 text-xs text-slate-500">The policy takes effect no earlier than this; empty means at publication.</p>
          </div>

          {!existing && (
            <div className="rounded-xl border border-slate-200 p-4">
              <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <input type="checkbox" checked={withArtifacts} onChange={(event) => setWithArtifacts(event.target.checked)} />
                Enter the installers now
              </label>
              <p className="mt-1 text-xs text-slate-500">
                Otherwise the release pipeline registers them. Each installer must live on an approved
                release host and carry the SHA-256 the desktop verifies before it runs anything.
              </p>
              {withArtifacts && (
                <div className="mt-4 space-y-4">
                  {slots.map((slot) => (
                    <div key={slot.key} className="rounded-lg bg-slate-50 p-3">
                      <div className="mb-2 text-xs font-bold text-slate-700">{slot.label} <span className="font-normal text-slate-400">({slot.hint})</span></div>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <div className="sm:col-span-2">
                          <label htmlFor={`artifact-${slot.key}-url`} className={labelClass}>Download URL</label>
                          <input
                            id={`artifact-${slot.key}-url`} value={artifacts[slot.key].download_url}
                            onChange={(event) => setArtifact(slot.key, { download_url: event.target.value })}
                            aria-invalid={errors[`artifact.${slot.key}.url`] ? true : undefined} className={inputClass}
                          />
                          <FieldError message={errors[`artifact.${slot.key}.url`]} />
                        </div>
                        <div className="sm:col-span-2">
                          <label htmlFor={`artifact-${slot.key}-sha`} className={labelClass}>SHA-256</label>
                          <input
                            id={`artifact-${slot.key}-sha`} value={artifacts[slot.key].sha256}
                            onChange={(event) => setArtifact(slot.key, { sha256: event.target.value })}
                            aria-invalid={errors[`artifact.${slot.key}.sha256`] ? true : undefined} className={inputClass}
                          />
                          <FieldError message={errors[`artifact.${slot.key}.sha256`]} />
                        </div>
                        <div>
                          <label htmlFor={`artifact-${slot.key}-size`} className={labelClass}>Size (bytes)</label>
                          <input
                            id={`artifact-${slot.key}-size`} value={artifacts[slot.key].file_size} inputMode="numeric"
                            onChange={(event) => setArtifact(slot.key, { file_size: event.target.value })}
                            aria-invalid={errors[`artifact.${slot.key}.size`] ? true : undefined} className={inputClass}
                          />
                          <FieldError message={errors[`artifact.${slot.key}.size`]} />
                        </div>
                        <label className="flex items-end gap-2 pb-2 text-sm font-semibold text-slate-700">
                          <input
                            type="checkbox" checked={artifacts[slot.key].signed}
                            onChange={(event) => setArtifact(slot.key, { signed: event.target.checked })}
                          />
                          Signature verified
                        </label>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-100 px-6 py-4">
          <button type="button" onClick={onClose} className={`${buttonClass} border border-slate-200 bg-white text-slate-600 hover:bg-slate-50`}>
            Cancel
          </button>
          <button type="submit" disabled={creating || updating} className={`${buttonClass} bg-[#2563EB] text-white hover:bg-[#1d4ed8]`}>
            {existing ? "Save changes" : "Create draft"}
          </button>
        </div>
      </form>
    </div>
  );
};

const Stat: React.FC<{ label: string; value: React.ReactNode; hint?: string }> = ({ label, value, hint }) => (
  <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
    <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500">{label}</div>
    <div className="mt-1 text-lg font-black text-slate-800">{value}</div>
    {hint && <div className="text-xs text-slate-500">{hint}</div>}
  </div>
);

const counts = (record: Record<string, number>, keys: string[]) =>
  keys.map((key) => `${record[key] ?? 0} ${key}`).join(" · ");

const ReleaseDetail: React.FC<{ version: string; onEdit: (release: ReleaseVersionDetail) => void }> = ({ version, onEdit }) => {
  const { showToast, confirmAction } = useFeedback();
  const detail = useGetReleaseVersionQuery(version);
  const [validate, { isLoading: validating }] = useValidateReleaseVersionMutation();
  const [publish, { isLoading: publishing }] = usePublishReleaseVersionMutation();
  const [withdraw, { isLoading: withdrawing }] = useWithdrawReleaseVersionMutation();
  const [error, setError] = useState<string | null>(null);

  if (detail.isLoading) return <Spinner label="Loading release…" />;
  if (detail.isError || !detail.data) return <ErrorNote message="Could not load this release." onRetry={() => detail.refetch()} />;
  const release = detail.data;
  const live = release.status === "published" || release.status === "superseded";
  const canPublish = (release.status === "draft" || release.status === "validated") && release.ready;

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      showToast(label, "success");
    } catch (exc) {
      const message = formatApiError((exc as { data?: unknown })?.data, "The release could not be changed.");
      setError(message);
      showToast(message, "error");
    }
  };

  const onPublish = async () => {
    const confirmed = await confirmAction(
      `Publish Monitra ${release.version}?`,
      release.mandatory
        ? `Every ${platformWords(release.target_platforms)} desktop below ${release.enforced_floor ?? release.version} will be required to update. Each user gets ${release.grace_hours} hours from the first time their desktop sees it; after that Monitra cannot be used on the old version. The announcement email is queued now.`
        : `Every ${platformWords(release.target_platforms)} desktop will be offered this version. It is optional: nobody is blocked. The announcement email is queued now.`,
    );
    if (!confirmed) return;
    await act("Release published.", () => publish(release.version).unwrap());
  };

  const onWithdraw = async () => {
    const confirmed = await confirmAction(
      `Withdraw Monitra ${release.version}?`,
      "Desktops stop being offered it at once and the previous published version is newest again. A grace period already running is kept, not erased: enforcing it is suspended while nothing installable exists for its floor, and a corrective release with the same floor resumes it with the original deadline.",
    );
    if (!confirmed) return;
    await act("Release withdrawn.", () => withdraw({ version: release.version }).unwrap());
  };

  return (
    <div className="space-y-5">
      {error && <ErrorNote message={error} />}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-black text-slate-800">Monitra {release.version}</h3>
            <StatusPill status={release.status} />
          </div>
          {release.title && <div className="text-sm font-semibold text-slate-600">{release.title}</div>}
          <div className="mt-1 text-xs text-slate-500">
            {platformWords(release.target_platforms)} · {policyWords(release)}
            {release.effective_at ? ` · effective ${formatWhen(release.effective_at)}` : ""}
          </div>
          <div className="mt-1 text-xs text-slate-500">
            Created {formatWhen(release.created_at)} · Published {formatWhen(release.published_at)}
            {release.withdrawn_at ? ` · Withdrawn ${formatWhen(release.withdrawn_at)}` : ""}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {release.status !== "withdrawn" && (
            <button type="button" onClick={() => onEdit(release)} className={`${buttonClass} border border-slate-200 bg-white text-slate-600 hover:bg-slate-50`}>
              Edit
            </button>
          )}
          {!live && release.status !== "withdrawn" && (
            <button
              type="button" disabled={validating}
              onClick={() => act(release.ready ? "Release validated." : "Validation found problems.", () => validate(release.version).unwrap())}
              className={`${buttonClass} border border-sky-200 bg-sky-50 text-sky-700 hover:bg-sky-100`}
            >
              Validate
            </button>
          )}
          {!live && release.status !== "withdrawn" && (
            <button
              type="button" disabled={!canPublish || publishing} onClick={onPublish}
              className={`${buttonClass} bg-[#2563EB] text-white hover:bg-[#1d4ed8]`}
              title={canPublish ? undefined : "Register every installer for the platforms this release targets first."}
            >
              Publish
            </button>
          )}
          {live && (
            <button type="button" disabled={withdrawing} onClick={onWithdraw} className={`${buttonClass} bg-rose-600 text-white hover:bg-rose-700`}>
              Withdraw
            </button>
          )}
        </div>
      </div>

      {release.problems.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          <div className="font-bold">Not ready to publish</div>
          <ul className="mt-1 list-disc pl-5">
            {release.problems.map((problem, index) => (
              <li key={`${problem.code}-${index}`}>{problem.artifact ? `${problem.artifact}: ` : ""}{problem.message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Stat label="Targeted users" value={release.stats.targeted_users} hint={`active desktop users on ${platformWords(release.target_platforms)}`} />
        <Stat label="Grace periods" value={counts(release.stats.requirements, ["pending", "expired", "satisfied"])} hint={release.mandatory ? "started when each desktop first met the policy" : "none for an optional release"} />
        <Stat label="Adoption" value={`${release.stats.adoption.on_or_above} on or above · ${release.stats.adoption.below} below`} hint="by last-seen desktop version" />
        <Stat label="Installations reported" value={counts(release.stats.installs, ["success", "failed"])} hint={Object.keys(release.stats.installs).length ? Object.entries(release.stats.installs).map(([k, v]) => `${v} ${k}`).join(", ") : "nothing reported yet"} />
        <Stat label="Announcement email" value={counts(release.stats.email, ["pending", "sent", "failed"])} hint='"sent" is provider acceptance, not confirmed delivery' />
      </div>

      <div>
        <h4 className="mb-2 text-sm font-bold text-slate-700">Installers</h4>
        {release.artifacts.length === 0 ? (
          <EmptyState message="No installers registered yet." hint="The release pipeline registers them, or enter them when creating the release." />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] font-bold uppercase tracking-wider text-slate-500">
                <th className="py-1 pr-3">Platform</th><th className="py-1 pr-3">File</th><th className="py-1 pr-3">Size</th><th className="py-1 pr-3">Signed</th><th className="py-1">Status</th>
              </tr>
            </thead>
            <tbody>
              {release.artifacts.map((artifact) => (
                <tr key={artifact.id} className="border-t border-slate-100">
                  <td className="py-1.5 pr-3 font-semibold text-slate-700">{PLATFORM_LABELS[artifact.platform as ReleasePlatform] ?? artifact.platform}{artifact.architecture ? ` ${artifact.architecture}` : ""}</td>
                  <td className="py-1.5 pr-3 text-slate-600" title={artifact.download_url}>{artifact.download_url.split("/").pop()}</td>
                  <td className="py-1.5 pr-3 text-slate-600">{artifact.file_size ? `${(artifact.file_size / 1048576).toFixed(1)} MB` : "—"}</td>
                  <td className="py-1.5 pr-3 text-slate-600">{artifact.signed ? (artifact.signer ?? "yes") : "no"}</td>
                  <td className="py-1.5"><StatusPill status={artifact.status === "rolled_back" ? "withdrawn" : artifact.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <h4 className="mb-2 text-sm font-bold text-slate-700">History</h4>
        {release.history.length === 0 ? (
          <EmptyState message="No actions recorded." />
        ) : (
          <ul className="space-y-1 text-sm">
            {release.history.map((entry) => (
              <li key={entry.id} className="flex gap-3 border-t border-slate-100 py-1.5">
                <span className="w-40 shrink-0 text-xs text-slate-500">{formatWhen(entry.created_at)}</span>
                <span className="w-20 shrink-0 text-xs font-bold text-slate-700">{AUDIT_ACTION_LABELS[entry.action] ?? entry.action}</span>
                <span className="text-slate-600">{entry.description}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
};

export const AdminDesktopReleases: React.FC = () => {
  const versions = useGetReleaseVersionsQuery();
  const [editing, setEditing] = useState<Editing | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const list = useMemo(() => versions.data?.versions ?? [], [versions.data]);
  const current = selected ?? list[0]?.version ?? null;

  return (
    <V2Shell
      title="Settings"
      subtitle="Desktop Releases"
      actions={
        <button
          type="button" onClick={() => setEditing({ kind: "new" })}
          className={`${buttonClass} bg-[#2563EB] px-4 py-2 text-sm text-white hover:bg-[#1d4ed8]`}
        >
          + New release
        </button>
      }
    >
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <Card
          title="Releases"
          action={<InlineRefreshIndicator active={versions.isFetching && !versions.isLoading} label="Refreshing" />}
        >
          {versions.isLoading ? (
            <Spinner label="Loading releases…" />
          ) : versions.isError ? (
            <ErrorNote message="Could not load the releases. Only administrators can open this page." onRetry={() => versions.refetch()} />
          ) : list.length === 0 ? (
            <EmptyState message="No releases yet." hint="Create one, or let the release pipeline register the next build." />
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] font-bold uppercase tracking-wider text-slate-500">
                  <th className="py-2 pr-3">Version</th><th className="py-2 pr-3">Platforms</th><th className="py-2 pr-3">Policy</th><th className="py-2 pr-3">Published</th><th className="py-2 pr-3">Ready</th><th className="py-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {list.map((release) => (
                  <tr
                    key={release.id} onClick={() => setSelected(release.version)}
                    className={`cursor-pointer border-t border-slate-100 ${current === release.version ? "bg-sky-50/60" : "hover:bg-slate-50"}`}
                    aria-selected={current === release.version}
                  >
                    <td className="py-2 pr-3 font-bold text-slate-800">{release.version}{release.title ? <span className="ml-2 font-normal text-slate-500">{release.title}</span> : null}</td>
                    <td className="py-2 pr-3 text-slate-600">{platformWords(release.target_platforms)}</td>
                    <td className="py-2 pr-3 text-slate-600">{policyWords(release)}</td>
                    <td className="py-2 pr-3 text-slate-600">{formatWhen(release.published_at)}</td>
                    <td className="py-2 pr-3 text-slate-600">{release.ready ? "yes" : `${release.problems.length} problem${release.problems.length === 1 ? "" : "s"}`}</td>
                    <td className="py-2"><StatusPill status={release.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        {current && (
          <Card title="Release">
            <ReleaseDetail version={current} onEdit={(release) => setEditing({ kind: "edit", release })} />
          </Card>
        )}
      </div>
      {editing && <ReleaseDialog editing={editing} onClose={() => setEditing(null)} />}
    </V2Shell>
  );
};
