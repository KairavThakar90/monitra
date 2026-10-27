import React, { useEffect, useMemo, useRef, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Card, ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { Pagination } from "../../components/Pagination";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import { FieldError, FormErrorBanner, SEARCH_MAX_LENGTH, validateInteger, validateSearchTerm } from "../../validation";
import { useGetAllMembersQuery, type Member } from "../../store/api/membersApi";
import {
  isCapturableMember,
  screenshotsAreOn,
  useGetScreenshotSettingsQuery,
  useSetMemberScreenshotsMutation,
  useSetScreenshotsForMembersMutation,
  useUpdateScreenshotSettingsMutation,
  type ScreenshotMemberScope,
  type ScreenshotSettings,
} from "../../store/api/screenshotSettingsApi";

/**
 * Admin Settings -> Screenshot Management.
 *
 * Two things an administrator decides about the screenshots every desktop takes:
 *
 * 1. who is captured at all -- a switch per member (on unless explicitly off),
 *    plus "all" / "selected" in one request;
 * 2. what a screenshot looks like -- its size and its WebP quality, one
 *    deployment-wide pair that is saved together.
 *
 * Every limit shown or enforced here comes from the settings response, so the
 * page cannot drift from the server. The server stays the final authority: this
 * page only refuses what it would refuse, never more. Clients and the release
 * service account are not captured and are never listed.
 */

const inputClass =
  "w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";
const labelClass = "mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500";
const bulkButton =
  "rounded-lg border px-3 py-2 text-[12px] font-bold transition disabled:cursor-not-allowed disabled:opacity-50";
const QUALITY_PRESETS = [50, 75, 90];

const formatWhen = (iso: string | null | undefined) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

const isOn = screenshotsAreOn;
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
const errorStatus = (error: unknown) => (error as { status?: unknown } | undefined)?.status;
const errorData = (error: unknown) => (error as { data?: unknown } | undefined)?.data;

/* ------------------------------------------------------------------ */
/* Size and quality                                                    */
/* ------------------------------------------------------------------ */

type QualityMode = "auto" | "fixed";

export const SizeAndQuality: React.FC<{ settings: ScreenshotSettings }> = ({ settings }) => {
  const { showToast } = useFeedback();
  const [save, { isLoading: saving }] = useUpdateScreenshotSettingsMutation();

  const [width, setWidth] = useState(String(settings.image_width));
  const [height, setHeight] = useState(String(settings.image_height));
  const [mode, setMode] = useState<QualityMode>(settings.image_quality === null ? "auto" : "fixed");
  const [quality, setQuality] = useState(settings.image_quality === null ? "" : String(settings.image_quality));
  const [formError, setFormError] = useState<string | null>(null);

  // Whatever the server holds is the truth: after a save, or when another
  // administrator changed it, the fields show that. Keyed on the values, so a
  // background refetch that returns the same thing never overwrites typing.
  const serverKey = `${settings.version}|${settings.image_width}|${settings.image_height}|${settings.image_quality}`;
  const syncedKey = useRef(serverKey);
  useEffect(() => {
    if (syncedKey.current === serverKey) return;
    syncedKey.current = serverKey;
    setWidth(String(settings.image_width));
    setHeight(String(settings.image_height));
    setMode(settings.image_quality === null ? "auto" : "fixed");
    setQuality(settings.image_quality === null ? "" : String(settings.image_quality));
  }, [serverKey, settings.image_width, settings.image_height, settings.image_quality]);

  const edge = { minimum: settings.min_image_edge, maximum: settings.max_image_edge };
  const widthCheck = validateInteger(width, { fieldLabel: "Width", ...edge });
  const heightCheck = validateInteger(height, { fieldLabel: "Height", ...edge });
  const qualityCheck = validateInteger(quality, {
    fieldLabel: "Quality",
    minimum: settings.min_image_quality,
    maximum: settings.max_image_quality,
  });

  // Prefilled fields are valid, so an error means the administrator typed it.
  // The quality box starts empty when switching to Fixed: it only complains once it has text.
  const widthError = widthCheck.ok ? null : widthCheck.error;
  const heightError = heightCheck.ok ? null : heightCheck.error;
  const qualityError = mode === "fixed" && quality.trim() !== "" && !qualityCheck.ok ? qualityCheck.error : null;

  const qualityValue: number | null | undefined =
    mode === "auto" ? null : qualityCheck.ok ? qualityCheck.value : undefined;
  const valid = widthCheck.ok && heightCheck.ok && qualityValue !== undefined;
  const dirty =
    valid &&
    (widthCheck.value !== settings.image_width ||
      heightCheck.value !== settings.image_height ||
      qualityValue !== settings.image_quality);

  const atDefaultSize =
    width.trim() === String(settings.default_image_width) && height.trim() === String(settings.default_image_height);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!valid || !dirty || saving) return;
    setFormError(null);
    try {
      await save({
        image_width: (widthCheck as { value: number }).value,
        image_height: (heightCheck as { value: number }).value,
        // Automatic is an explicit null, never an omitted key: the PUT is a replacement.
        image_quality: qualityValue as number | null,
      }).unwrap();
      showToast("Screenshot settings saved.", "success");
    } catch (exc) {
      const message = formatApiError(errorData(exc), "Could not save the screenshot settings.");
      setFormError(message);
      showToast(message, "error");
    }
  };

  const describedBy = (id: string, error: string | null) => (error ? `${id}-error` : `${id}-hint`);

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6" aria-label="Screenshot size and quality">
      <Card title="Screenshot size">
        <p className="mb-4 text-sm text-slate-600">
          The size of every screenshot, in pixels. The screen is scaled to fit inside it, keeping its proportions, and
          any space left over is filled with grey.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="screenshot-width" className={labelClass}>
              Width (px)
            </label>
            <input
              id="screenshot-width"
              type="text"
              inputMode="numeric"
              autoComplete="off"
              value={width}
              onChange={(event) => setWidth(event.target.value)}
              aria-invalid={widthError ? true : undefined}
              aria-describedby={describedBy("screenshot-width", widthError)}
              className={inputClass + (widthError ? " border-rose-300" : " border-slate-200")}
            />
            <p id="screenshot-width-hint" className="mt-1 text-xs text-slate-500">
              {settings.min_image_edge}&ndash;{settings.max_image_edge} px
            </p>
            <FieldError id="screenshot-width-error" message={widthError} />
          </div>
          <div>
            <label htmlFor="screenshot-height" className={labelClass}>
              Height (px)
            </label>
            <input
              id="screenshot-height"
              type="text"
              inputMode="numeric"
              autoComplete="off"
              value={height}
              onChange={(event) => setHeight(event.target.value)}
              aria-invalid={heightError ? true : undefined}
              aria-describedby={describedBy("screenshot-height", heightError)}
              className={inputClass + (heightError ? " border-rose-300" : " border-slate-200")}
            />
            <p id="screenshot-height-hint" className="mt-1 text-xs text-slate-500">
              {settings.min_image_edge}&ndash;{settings.max_image_edge} px
            </p>
            <FieldError id="screenshot-height-error" message={heightError} />
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>
            Default: {settings.default_image_width} &times; {settings.default_image_height}
          </span>
          <button
            type="button"
            disabled={atDefaultSize}
            onClick={() => {
              setWidth(String(settings.default_image_width));
              setHeight(String(settings.default_image_height));
            }}
            className="font-bold text-[#2563EB] hover:underline disabled:cursor-not-allowed disabled:text-slate-400 disabled:no-underline"
          >
            Reset to default
          </button>
        </div>
      </Card>

      <Card title="Screenshot quality">
        <fieldset>
          <legend className="sr-only">Screenshot quality</legend>
          <div className="space-y-3">
            <label className="flex cursor-pointer items-start gap-2.5 text-sm font-semibold text-slate-700">
              <input
                type="radio"
                name="screenshot-quality-mode"
                checked={mode === "auto"}
                onChange={() => {
                  setMode("auto");
                  setFormError(null);
                }}
                className="mt-0.5 h-4 w-4 border-slate-300 text-blue-600 focus:ring-blue-500/40"
              />
              <span>
                Automatic (recommended)
                <span className="block text-xs font-normal text-slate-500">
                  Starts at {settings.automatic_image_quality}% and is lowered on busy screens to keep files small.
                </span>
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2.5 text-sm font-semibold text-slate-700">
              <input
                type="radio"
                name="screenshot-quality-mode"
                checked={mode === "fixed"}
                onChange={() => {
                  setMode("fixed");
                  setFormError(null);
                }}
                className="mt-0.5 h-4 w-4 border-slate-300 text-blue-600 focus:ring-blue-500/40"
              />
              <span>
                Fixed quality
                <span className="block text-xs font-normal text-slate-500">
                  Screenshots are saved at this quality. Higher is sharper, but the files are larger and use more
                  storage. (Only a file too large to upload is saved at a lower quality.)
                </span>
              </span>
            </label>
          </div>
        </fieldset>

        {mode === "fixed" && (
          <div className="mt-4 max-w-xs">
            <label htmlFor="screenshot-quality" className={labelClass}>
              Quality (%)
            </label>
            <input
              id="screenshot-quality"
              type="text"
              inputMode="numeric"
              autoComplete="off"
              value={quality}
              placeholder={`${settings.min_image_quality}–${settings.max_image_quality}`}
              onChange={(event) => setQuality(event.target.value)}
              aria-invalid={qualityError ? true : undefined}
              aria-describedby={describedBy("screenshot-quality", qualityError)}
              className={inputClass + (qualityError ? " border-rose-300" : " border-slate-200")}
            />
            <p id="screenshot-quality-hint" className="mt-1 text-xs text-slate-500">
              {settings.min_image_quality}&ndash;{settings.max_image_quality}%
            </p>
            <FieldError id="screenshot-quality-error" message={qualityError} />
            <div className="mt-2 flex flex-wrap gap-1.5" role="group" aria-label="Quick quality picks">
              {QUALITY_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  aria-pressed={quality.trim() === String(preset)}
                  onClick={() => setQuality(String(preset))}
                  className={
                    "rounded-full border px-3 py-1 text-[12px] font-bold transition " +
                    (quality.trim() === String(preset)
                      ? "border-[#2563EB] bg-[#2563EB] text-white"
                      : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50")
                  }
                >
                  {preset}%
                </button>
              ))}
            </div>
          </div>
        )}
      </Card>

      <div className="flex flex-col gap-3 rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
        <FormErrorBanner message={formError} />
        <p className="text-xs leading-5 text-slate-500">
          Size and quality apply to screenshots captured from now on. Desktops pick up a change within a few minutes.
          Screenshots already stored keep the size they were taken at.
        </p>
        {settings.version > 0 && settings.updated_by_username && (
          <p className="text-xs text-slate-500">
            Last changed by <span className="font-semibold text-slate-700">{settings.updated_by_username}</span>
            {settings.updated_at ? ` on ${formatWhen(settings.updated_at)}` : ""}.
          </p>
        )}
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={!valid || !dirty || saving}
            className="rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-5 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
          {mode === "fixed" && quality.trim() === "" && (
            <span className="text-xs text-slate-500">Enter a quality percentage to save.</span>
          )}
        </div>
      </div>
    </form>
  );
};

/* ------------------------------------------------------------------ */
/* Member-wise control                                                 */
/* ------------------------------------------------------------------ */

const MemberSwitch: React.FC<{ member: Member; busy: boolean; onChange: (next: boolean) => void }> = ({
  member,
  busy,
  onChange,
}) => {
  const on = isOn(member);
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={`Screenshots for ${member.name}`}
        disabled={busy}
        onClick={() => onChange(!on)}
        className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:cursor-not-allowed disabled:opacity-60 ${
          on ? "bg-blue-600" : "bg-slate-300"
        }`}
      >
        <span
          className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
            on ? "translate-x-5" : "translate-x-0.5"
          }`}
        />
      </button>
      <span className={"text-xs font-bold " + (on ? "text-blue-700" : "text-slate-500")}>{on ? "On" : "Off"}</span>
    </div>
  );
};

/**
 * `embedded` is for use inside an accordion section that already carries the
 * heading, so the card does not repeat it; the heading stays for screen readers.
 */
export const MemberControl: React.FC<{ embedded?: boolean }> = ({ embedded = false }) => {
  const { showToast, confirmAction } = useFeedback();
  const members = useGetAllMembersQuery();
  const [setMember] = useSetMemberScreenshotsMutation();
  const [setMany, { isLoading: bulkBusy }] = useSetScreenshotsForMembersMutation();

  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [pending, setPending] = useState<Set<number>>(new Set());
  const [selected, setSelected] = useState<Set<number>>(new Set());

  // The people a desktop can capture: active members, never a client or the release service account.
  const capturable = useMemo(() => (members.data ?? []).filter(isCapturableMember), [members.data]);

  const searchCheck = validateSearchTerm(search, { fieldLabel: "Search" });
  const term = searchCheck.ok ? searchCheck.value.trim().toLowerCase() : "";
  const filtered = useMemo(
    () =>
      term
        ? capturable.filter((m) => `${m.name} ${m.email}`.toLowerCase().includes(term))
        : capturable,
    [capturable, term],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const rows = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const onCount = capturable.filter(isOn).length;
  const rowsSelected = rows.filter((m) => selected.has(m.id)).length;
  const allRowsSelected = rows.length > 0 && rowsSelected === rows.length;
  // A member who left (or was filtered into a client) must not stay selected for a bulk change.
  const selectedIds = useMemo(
    () => capturable.filter((m) => selected.has(m.id)).map((m) => m.id),
    [capturable, selected],
  );

  const toggle = async (member: Member, enabled: boolean) => {
    setPending((current) => new Set(current).add(member.id));
    try {
      await setMember({ id: member.id, enabled }).unwrap();
    } catch (exc) {
      // The switch has already been put back by the mutation.
      showToast(
        formatApiError(errorData(exc), `Could not turn screenshots ${enabled ? "on" : "off"} for ${member.name}.`),
        "error",
      );
    } finally {
      setPending((current) => {
        const next = new Set(current);
        next.delete(member.id);
        return next;
      });
    }
  };

  const bulk = async (enabled: boolean, apply_to: ScreenshotMemberScope, who: string, title: string) => {
    const verb = enabled ? "on" : "off";
    const confirmed = await confirmAction(
      title,
      `Screenshots will be turned ${verb} for ${who}. Screenshots already taken are not deleted. ` +
        `A change reaches each member's desktop within a few minutes.`,
    );
    if (!confirmed) return;
    try {
      const result = await setMany({ enabled, apply_to }).unwrap();
      if (result.matched === 0) {
        showToast("There were no active members to change.", "success");
      } else if (result.changed === 0) {
        showToast(`Screenshots were already ${verb} for ${plural(result.matched, "member", "members")}.`, "success");
      } else {
        showToast(
          `Screenshots turned ${verb} for ${plural(result.changed, "member", "members")}` +
            (result.matched > result.changed ? ` (${result.matched - result.changed} already ${verb}).` : "."),
          "success",
        );
      }
      if (apply_to.scope === "members") setSelected(new Set());
    } catch (exc) {
      showToast(formatApiError(errorData(exc), "Could not change screenshots for those members."), "error");
    }
  };

  const toggleRow = (id: number) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const toggleRows = () =>
    setSelected((current) => {
      const next = new Set(current);
      rows.forEach((m) => (allRowsSelected ? next.delete(m.id) : next.add(m.id)));
      return next;
    });

  const noMembers = !members.isLoading && !members.isError && capturable.length === 0;

  return (
    <section aria-labelledby="member-screenshots-heading">
      <Card
        title={embedded ? undefined : "Member-wise screenshot control"}
        action={<InlineRefreshIndicator active={members.isFetching && !members.isLoading} label="Checking" />}
      >
        <h2 id="member-screenshots-heading" className="sr-only">
          Member-wise screenshot control
        </h2>
        <p className="mb-4 text-sm text-slate-600">
          Choose whose screen is captured. Screenshots are on for every member unless you turn them off. A change reaches
          a member&apos;s desktop within a few minutes. Screenshots already captured are kept.
        </p>

        {members.isLoading ? (
          <Spinner label="Loading members…" />
        ) : members.isError ? (
          <ErrorNote message="Could not load the members." onRetry={() => void members.refetch()} />
        ) : noMembers ? (
          <p className="rounded-xl border-2 border-dashed border-slate-200 px-6 py-10 text-center text-sm font-semibold text-slate-500">
            There are no active members yet.
          </p>
        ) : (
          <>
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <div className="min-w-[220px] flex-1">
                <input
                  type="text"
                  aria-label="Search members"
                  placeholder="Search members by name or email..."
                  value={search}
                  maxLength={SEARCH_MAX_LENGTH}
                  aria-invalid={searchError ? true : undefined}
                  aria-describedby={searchError ? "screenshot-member-search-error" : undefined}
                  onChange={(event) => {
                    const next = event.target.value;
                    setSearch(next);
                    const result = validateSearchTerm(next, { fieldLabel: "Search" });
                    setSearchError(result.ok ? null : result.error);
                    setPage(1);
                  }}
                  className={inputClass + (searchError ? " border-rose-300" : " border-slate-200")}
                />
                <FieldError id="screenshot-member-search-error" message={searchError} />
              </div>
              <span className="text-xs font-semibold text-slate-500" data-testid="on-count">
                {onCount} of {plural(capturable.length, "active member", "active members")} on
              </span>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() =>
                    bulk(true, { scope: "all" }, `all ${capturable.length} active members`, "Turn on screenshots for all members?")
                  }
                  className={bulkButton + " border-blue-600 bg-blue-600 text-white hover:bg-blue-700"}
                >
                  Turn on for all
                </button>
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() =>
                    bulk(false, { scope: "all" }, `all ${capturable.length} active members`, "Turn off screenshots for all members?")
                  }
                  className={bulkButton + " border-slate-200 bg-white text-slate-700 hover:bg-slate-50"}
                >
                  Turn off for all
                </button>
              </div>
            </div>

            {selectedIds.length > 0 && (
              <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-blue-100 bg-blue-50/60 px-4 py-3">
                <span className="text-sm font-bold text-slate-800">{selectedIds.length} selected</span>
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() =>
                    bulk(
                      true,
                      { scope: "members", user_ids: selectedIds },
                      plural(selectedIds.length, "selected member", "selected members"),
                      "Turn on screenshots for the selected members?",
                    )
                  }
                  className={bulkButton + " border-blue-600 bg-blue-600 text-white hover:bg-blue-700"}
                >
                  Turn on for selected
                </button>
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() =>
                    bulk(
                      false,
                      { scope: "members", user_ids: selectedIds },
                      plural(selectedIds.length, "selected member", "selected members"),
                      "Turn off screenshots for the selected members?",
                    )
                  }
                  className={bulkButton + " border-slate-200 bg-white text-slate-700 hover:bg-slate-50"}
                >
                  Turn off for selected
                </button>
                <button
                  type="button"
                  onClick={() => setSelected(new Set())}
                  className="ml-auto text-[11px] font-bold uppercase tracking-wider text-slate-500 hover:text-slate-700"
                >
                  Clear selection
                </button>
              </div>
            )}

            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-slate-500">
                  <tr>
                    <th className="w-10 py-3 pl-4">
                      <input
                        type="checkbox"
                        aria-label="Select all members on this page"
                        checked={allRowsSelected}
                        ref={(el) => {
                          if (el) el.indeterminate = rowsSelected > 0 && !allRowsSelected;
                        }}
                        disabled={rows.length === 0}
                        onChange={toggleRows}
                        className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
                      />
                    </th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Member</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Role</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Screenshots</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="px-6 py-10 text-center text-sm font-semibold text-slate-500">
                        No members match your search.
                      </td>
                    </tr>
                  ) : (
                    rows.map((member) => (
                      <tr key={member.id} className={selected.has(member.id) ? "bg-blue-50/40" : ""}>
                        <td className="w-10 py-3 pl-4">
                          <input
                            type="checkbox"
                            aria-label={`Select ${member.name}`}
                            checked={selected.has(member.id)}
                            onChange={() => toggleRow(member.id)}
                            className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
                          />
                        </td>
                        <td className="px-4 py-3">
                          <div className="font-bold text-slate-800">{member.name || "-"}</div>
                          <div className="text-xs text-slate-500">{member.email}</div>
                        </td>
                        <td className="px-4 py-3 capitalize text-slate-600">{(member.role || "").replace(/_/g, " ")}</td>
                        <td className="px-4 py-3">
                          <MemberSwitch
                            member={member}
                            busy={pending.has(member.id)}
                            onChange={(next) => void toggle(member, next)}
                          />
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            <Pagination
              page={currentPage}
              totalPages={totalPages}
              totalItems={filtered.length}
              limit={pageSize}
              setPage={setPage}
              setLimit={setPageSize}
              noun="members"
              className="mt-4"
            />
          </>
        )}
      </Card>
    </section>
  );
};

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export const AdminScreenshotManagement: React.FC = () => {
  const settings = useGetScreenshotSettingsQuery();
  const data = settings.data;
  const forbidden = errorStatus(settings.error) === 403;

  return (
    <V2Shell title="Settings" subtitle="Screenshot Management">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-8">
        <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 text-sm leading-6 text-slate-600 shadow-sm">
          <p className="font-semibold text-slate-800">Decide who is captured, and what a screenshot looks like.</p>
          <p>
            Turn screenshots on or off for each member, and set the size and quality every desktop captures at. Desktops
            pick up a change within a few minutes.
          </p>
        </div>

        {forbidden ? (
          <ErrorNote message="Only administrators can open this page." onRetry={null} />
        ) : (
          <>
            {/* Not before the settings answer: a non-administrator is refused there, and must not see members first. */}
            {!settings.isLoading && <MemberControl />}
            <section aria-labelledby="screenshot-look-heading" className="flex flex-col gap-3">
              <h2 id="screenshot-look-heading" className="sr-only">
                Screenshot size and quality
              </h2>
              {settings.isLoading ? (
                <Card>
                  <Spinner label="Loading screenshot settings…" />
                </Card>
              ) : settings.isError || !data ? (
                <ErrorNote message="Could not load the screenshot settings." onRetry={() => void settings.refetch()} />
              ) : (
                <SizeAndQuality settings={data} />
              )}
            </section>
          </>
        )}
      </div>
    </V2Shell>
  );
};
