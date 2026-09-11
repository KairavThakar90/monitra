import React from "react";
import { BRAND_MARKS } from "./appIconData";

/**
 * The icon for a tracked application.
 *
 * `time_entry_app_usage.application_name` is now the application's canonical
 * name -- "Google Chrome", "Visual Studio Code", "Microsoft Teams" -- resolved
 * by `desktop/tracking/app_identity.py` and again by the backend on ingest.
 * Rows written before that release still carry the raw executable stem
 * ("chrome", "Code", "WINWORD"), and an application outside the catalogue
 * keeps its stem on purpose, so both spellings reach this file and both are
 * recognised.
 *
 * There is no icon data anywhere in the system: the desktop extracts real
 * window icons for its own Activity view (`desktop/ui/icon_manager.py`) but
 * never uploads them, so the web client cannot show the machine's actual icon
 * for an app.
 *
 * What it can do is recognise the name. An application this file knows is
 * drawn with its own brand mark in its own colour; anything else gets a
 * neutral monogram tile. The fallback is deliberately *not* a guessed logo --
 * an unrecognised in-house tool must not be handed some other company's mark.
 */

/* ------------------------------------------------------------------ */
/* Marks Simple Icons does not carry                                   */
/* ------------------------------------------------------------------ */

/**
 * Microsoft, Visual Studio Code and Slack are absent from Simple Icons, so
 * their marks are drawn here.
 *
 * Most are `letter` tiles, which is not a compromise: the Office icons really
 * are a coloured tile carrying the app's initial, and Edge's mark really is a
 * stylised "e". Where a shape is simple and unmistakable -- the Windows panes,
 * a file-explorer folder, a terminal prompt -- it is drawn as a path instead.
 */
type HouseMark =
  | { hex: string; title: string; letter: string }
  | { hex: string; title: string; path: string };

const HOUSE_MARKS: Record<string, HouseMark> = {
  vscode: {
    hex: "#0065A9",
    title: "Visual Studio Code",
    // The folded ribbon, with the fold cut out of it by the closing subpath
    // (hence `fill-rule: evenodd` on the rendered path).
    path: "M23.15 2.587 18.21.21a1.494 1.494 0 0 0-1.705.29l-9.46 8.63-4.12-3.128a.999.999 0 0 0-1.276.057L.327 7.394A1 1 0 0 0 .326 8.88L3.899 12 .326 15.12a1 1 0 0 0 .001 1.486L1.65 17.94a.999.999 0 0 0 1.276.057l4.12-3.128 9.46 8.63a1.492 1.492 0 0 0 1.704.29l4.942-2.377A1.5 1.5 0 0 0 24 20.06V3.939a1.5 1.5 0 0 0-.85-1.352zm-5.146 14.861L10.826 12l7.178-5.448v10.896z",
  },
  edge: { hex: "#0F7EBF", title: "Microsoft Edge", letter: "e" },
  word: { hex: "#185ABD", title: "Microsoft Word", letter: "W" },
  excel: { hex: "#107C41", title: "Microsoft Excel", letter: "X" },
  powerpoint: { hex: "#C43E1C", title: "Microsoft PowerPoint", letter: "P" },
  outlook: { hex: "#0F6CBD", title: "Microsoft Outlook", letter: "O" },
  onenote: { hex: "#7719AA", title: "Microsoft OneNote", letter: "N" },
  teams: { hex: "#5059C9", title: "Microsoft Teams", letter: "T" },
  slack: { hex: "#4A154B", title: "Slack", letter: "S" },
  explorer: {
    hex: "#FFB900",
    title: "File Explorer",
    path: "M3 5.5A1.5 1.5 0 0 1 4.5 4h4.2l2 2.2h6.8A1.5 1.5 0 0 1 19 7.7v1H3v-3.2Zm0 4.7h18l-1.4 7.6A1.5 1.5 0 0 1 18.1 19H5.4a1.5 1.5 0 0 1-1.5-1.2L3 10.2Z",
  },
  windows: {
    hex: "#0078D4",
    title: "Windows",
    path: "M3 5.8 10.6 4.7v6.6H3V5.8Zm8.9-1.3L21 3.2v8.1h-9.1V4.5ZM3 12.7h7.6v6.6L3 18.2v-5.5Zm8.9 0H21v8.1l-9.1-1.3v-6.8Z",
  },
  /**
   * Monitra itself. The desktop client runs as a bare Python process in
   * development and as `Monitra.exe` once packaged, and both used to be
   * reported as an application called "python" — drawn, wrongly, with
   * Python's own logo. It is this product, so it gets this product's mark in
   * this product's colour rather than borrowing a language's.
   */
  monitra: { hex: "#3B82F6", title: "Monitra", letter: "M" },
  terminal: {
    hex: "#1F2937",
    title: "Terminal",
    path: "M3 4h18a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm3.2 4.5L5 9.8l2.6 2.5L5 14.8l1.2 1.3 3.9-3.8-3.9-3.8ZM12 14.6v1.8h6v-1.8h-6Z",
  },
};

/* ------------------------------------------------------------------ */
/* Process name -> mark                                                */
/* ------------------------------------------------------------------ */

/**
 * Which mark a process name resolves to.
 *
 * Keys are normalised names (see `normalize`) and values are either a
 * `BRAND_MARKS` slug or a `HOUSE_MARKS` key. Several keys point at one mark on
 * purpose: an app is reported under whatever its executable happens to be
 * called, and "msedge", "microsoftedge" and "edge" are all the same browser.
 */
const ALIASES: Record<string, string> = {
  // Browsers
  chrome: "googlechrome",
  googlechrome: "googlechrome",
  chromium: "googlechrome",
  msedge: "edge",
  edge: "edge",
  microsoftedge: "edge",
  msedgewebview2: "edge",
  firefox: "firefoxbrowser",
  mozillafirefox: "firefoxbrowser",
  opera: "opera",
  brave: "brave",
  bravebrowser: "brave",
  vivaldi: "vivaldi",

  // Editors and developer tools
  code: "vscode",
  vscode: "vscode",
  visualstudiocode: "vscode",
  cursor: "vscode",
  windsurf: "vscode",
  sublimetext: "sublimetext",
  subl: "sublimetext",
  idea: "intellijidea",
  idea64: "intellijidea",
  intellijidea: "intellijidea",
  pycharm: "pycharm",
  pycharm64: "pycharm",
  python: "python",
  pythonw: "python",
  py: "python",
  jupyter: "jupyter",
  jupyterlab: "jupyter",
  node: "nodedotjs",
  nodejs: "nodedotjs",
  docker: "docker",
  dockerdesktop: "docker",
  postman: "postman",
  github: "github",
  githubdesktop: "github",
  gitlab: "gitlab",

  // Terminals and shells
  cmd: "terminal",
  powershell: "terminal",
  pwsh: "terminal",
  windowsterminal: "terminal",
  wt: "terminal",
  conhost: "terminal",
  terminal: "terminal",
  bash: "terminal",
  gitbash: "terminal",
  mintty: "terminal",

  // Microsoft Office
  winword: "word",
  word: "word",
  microsoftword: "word",
  excel: "excel",
  microsoftexcel: "excel",
  powerpnt: "powerpoint",
  powerpoint: "powerpoint",
  outlook: "outlook",
  microsoftoutlook: "outlook",
  onenote: "onenote",
  teams: "teams",
  msteams: "teams",
  microsoftteams: "teams",

  // Windows shell. These are the OS itself rather than an app somebody chose
  // to use, but they are reported like any other window and deserve a name
  // the reader recognises.
  explorer: "explorer",
  fileexplorer: "explorer",
  windowsexplorer: "explorer",
  shellexperiencehost: "windows",
  searchhost: "windows",
  searchapp: "windows",
  startmenuexperiencehost: "windows",
  applicationframehost: "windows",
  textinputhost: "windows",
  systemsettings: "windows",
  lockapp: "windows",
  dwm: "windows",
  sihost: "windows",

  // Communication and collaboration
  slack: "slack",
  discord: "discord",
  telegram: "telegram",
  whatsapp: "whatsapp",
  zoom: "zoom",
  zoomworkplace: "zoom",
  googlemeet: "googlemeet",
  meet: "googlemeet",
  gmail: "gmail",
  notion: "notion",
  figma: "figma",
  jira: "jira",
  confluence: "confluence",
  trello: "trello",
  obsidian: "obsidian",
  linear: "linear",
  miro: "miro",
  loom: "loom",
  claude: "claude",
  docs: "googledocs",
  googledocs: "googledocs",
  googlesheets: "googlesheets",
  googledrive: "googledrive",

  // Media
  spotify: "spotify",
  vlc: "vlcmediaplayer",

  /* ---------------------------------------------------------------- */
  /* Canonical application names                                       */
  /* ---------------------------------------------------------------- */
  /**
   * Since the release that gave every activity record one canonical identity,
   * `application_name` is the product's real name rather than the executable
   * stem — "Visual Studio Code", not "Code". Most canonical names already
   * normalise onto a key above ("Google Chrome" -> `googlechrome`,
   * "Microsoft Teams" -> `microsoftteams`); the ones below do not, and without
   * them an application that used to show its own mark would silently drop to
   * a monogram tile. `__tests__/appIcon.test.ts` fails if any alias this file
   * recognises has a canonical name it does not.
   *
   * An application with no mark at all — Safari, Canva, an in-house tool —
   * still gets the neutral monogram, deliberately. The fallback has never been
   * a guessed logo.
   */
  jetbrainspycharm: "pycharm",
  jetbrainsintellijidea: "intellijidea",
  microsoftpowerpoint: "powerpoint",
  microsoftonenote: "onenote",
  commandprompt: "terminal",
  // The OS shell, reported under the names Windows itself uses for these.
  windowsshell: "windows",
  windowssearch: "windows",
  windowsstartmenu: "windows",
  windowssettings: "windows",
  windowslockscreen: "windows",
  windowstaskmanager: "windows",
  windowssecurity: "windows",
  monitra: "monitra",
};

/**
 * Reduce a reported process name to a lookup key.
 *
 * Strips a trailing `.exe`, drops everything that is not a letter or digit,
 * and lowercases the rest, so "WINWORD.EXE", "WinWord" and "winword" are one
 * key -- the desktop reports whatever casing the OS gives it.
 */
const normalize = (name: string) =>
  name.replace(/\.exe$/i, "").replace(/[^a-z0-9]/gi, "").toLowerCase();

/** A stable hue for an app we do not recognise, derived from its own name. */
const monogramHue = (name: string) => {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) % 360;
  return hash;
};

/** The first character, which is what the monogram tile shows. */
const monogramLetter = (name: string) => (name.trim()[0] || "?").toUpperCase();

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export const AppIcon: React.FC<{ name: string; size?: number; className?: string }> = ({
  name,
  size = 20,
  className = "",
}) => {
  const key = ALIASES[normalize(name)];
  const brand = key ? BRAND_MARKS[key] : undefined;
  const house = key && !brand ? HOUSE_MARKS[key] : undefined;
  const mark = brand ?? house;

  // The tile is square and never shrinks below its size in a flex row, and the
  // glyph sits at ~60% of it so every icon has the same optical weight
  // whichever branch drew it.
  const tile: React.CSSProperties = {
    width: size,
    height: size,
    borderRadius: Math.max(4, Math.round(size * 0.25)),
  };
  const glyph = Math.round(size * 0.62);

  if (!mark) {
    // Unrecognised: a neutral monogram, never somebody else's logo.
    const hue = monogramHue(name);
    return (
      <span
        aria-hidden="true"
        title={name}
        className={`inline-flex shrink-0 items-center justify-center font-bold ${className}`}
        style={{
          ...tile,
          background: `hsl(${hue} 70% 94%)`,
          color: `hsl(${hue} 55% 32%)`,
          fontSize: Math.round(size * 0.5),
          lineHeight: 1,
        }}
      >
        {monogramLetter(name)}
      </span>
    );
  }

  return (
    <span
      aria-hidden="true"
      title={mark.title}
      className={`inline-flex shrink-0 items-center justify-center ${className}`}
      style={{ ...tile, background: mark.hex }}
    >
      {"letter" in mark ? (
        <span
          style={{
            color: "#FFFFFF",
            fontSize: Math.round(size * 0.55),
            fontWeight: 700,
            lineHeight: 1,
          }}
        >
          {mark.letter}
        </span>
      ) : (
        <svg width={glyph} height={glyph} viewBox="0 0 24 24" fill="#FFFFFF" role="presentation">
          <path d={mark.path} fillRule="evenodd" clipRule="evenodd" />
        </svg>
      )}
    </span>
  );
};
