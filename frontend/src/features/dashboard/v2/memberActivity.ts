/**
 * Each member's average activity for the report's filters, for the card shown when a name is
 * hovered in the Reports page's Member Breakdown. It is the server's own figure (the same
 * duration-weighted average the page's "Avg. Activity" tile uses), never recomputed from the
 * rows beneath. `null` is a member with nothing activity-sampled -- manual time only, say --
 * which is not the same as 0%.
 */
export interface MemberActivity {
  isLoading: boolean;
  isError: boolean;
  byMember: Record<number, number | null>;
}

/** What the hover card says about one member's activity. */
export const describeActivity = (activity: MemberActivity, memberId: number): string => {
  if (activity.isError) return "Activity unavailable";
  if (activity.isLoading && !(memberId in activity.byMember)) return "Activity loading…";
  const percent = activity.byMember[memberId];
  return percent === null || percent === undefined ? "Activity not recorded" : `Activity ${percent}%`;
};
