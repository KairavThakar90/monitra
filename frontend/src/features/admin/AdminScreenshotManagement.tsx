import React, { useEffect, useRef, useState } from "react";
import { Card } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { formatApiError } from "../../api/utils";
import { FieldError, FormErrorBanner, validateInteger } from "../../validation";
import {
  useUpdateScreenshotSettingsMutation,
  type ScreenshotSettings,
} from "../../store/api/screenshotSettingsApi";

/**
 * Screenshot Management -> Size and quality.
 *
 * What a screenshot looks like: its size and its WebP quality, one
 * deployment-wide pair that is saved together. (Who is captured is no longer
 * decided here: it is a per-member switch in User Management.)
 *
 * Every limit shown or enforced here comes from the settings response, so the
 * page cannot drift from the server. The server stays the final authority: this
 * page only refuses what it would refuse, never more.
 */

const inputClass =
  "w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";
const labelClass = "mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500";
const QUALITY_PRESETS = [50, 75, 90];

const formatWhen = (iso: string | null | undefined) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

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
