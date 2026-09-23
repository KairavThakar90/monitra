import React from 'react';
import { MultiSelect } from '../dashboard/v2/filters';

/** The Project filter — same underlying `MultiSelect` the staff
 * `ProjectMultiSelect` wraps, just built from the client's own shared
 * projects (which don't carry the full staff `Project` shape). */
export const ClientProjectFilter: React.FC<{
  projects: { id: number; project_name: string }[];
  selected: string[];
  onChange: (ids: string[]) => void;
}> = ({ projects, selected, onChange }) => (
  <MultiSelect
    options={projects.map((p) => ({ id: String(p.id), label: p.project_name }))}
    selected={selected}
    onChange={onChange}
    allLabel="All projects"
    noun="project"
  />
);

/** The Member filter — same underlying `MultiSelect` the staff
 * `MemberMultiSelect` is built from, scoped to the members who show up on
 * this client's shared projects rather than the organization's directory. */
export const ClientMemberFilter: React.FC<{
  members: { id: number; name: string }[];
  selected: string[];
  onChange: (ids: string[]) => void;
}> = ({ members, selected, onChange }) => (
  <MultiSelect
    options={members.map((m) => ({ id: String(m.id), label: m.name }))}
    selected={selected}
    onChange={onChange}
    allLabel="All members"
    noun="member"
  />
);
