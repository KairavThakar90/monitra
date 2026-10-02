import React from 'react';

/** The line shown above every signed-in screen. Change it here and it changes everywhere. */
export const SCREEN_NOTICE = 'Note: Screen enhancement is currently in progress.';

/**
 * The note at the very top of every signed-in screen.
 *
 * Mounted once in each shell (`V2Shell`, `MemberShell`, `ClientShell`) as the
 * first thing in the main column, above the page header, so it appears on all
 * screens after login and on none before it. It is plain text in the normal
 * flow: it takes no focus, has no close button and covers nothing.
 */
export const ScreenNoticeBanner: React.FC = () => (
  <div
    data-testid="screen-notice"
    role="status"
    className="shrink-0 border-b border-rose-200 bg-rose-50 px-4 py-1.5 text-center text-xs font-bold text-red-600 lg:px-8"
  >
    {SCREEN_NOTICE}
  </div>
);
