/**
 * The optional category a project can be tagged with, as the API stores it in
 * `projects.category`. A project with no category is simply uncategorised —
 * `null`, never a third value.
 *
 * Mirrors `ProjectCategory` in `backend/app/schemas/project_management.py`.
 */
export type ProjectCategory = 'kyle' | 'st';

/** What the Create Project dropdown and the list filter offer, in order. */
export const PROJECT_CATEGORY_OPTIONS: { value: ProjectCategory; label: string }[] = [
  { value: 'kyle', label: 'Kyle Project' },
  { value: 'st', label: 'ST Project' },
];

/** What a person calls a category; `—` for an uncategorised project. */
export const projectCategoryLabel = (category: string | null | undefined): string =>
  PROJECT_CATEGORY_OPTIONS.find((option) => option.value === category)?.label ?? '—';
