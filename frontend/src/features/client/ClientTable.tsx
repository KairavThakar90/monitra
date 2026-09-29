import React from 'react';

/** The admin Team column's per-member chip colour, keyed on id the same way
 * (`AdminProjectManagement`'s `getColor`), so the same person gets the same
 * colour on both sides. */
const AVATAR_COLORS = ['bg-blue-500', 'bg-rose-500', 'bg-emerald-500', 'bg-amber-500', 'bg-purple-500', 'bg-cyan-500'];

/**
 * The overlapping avatar-initials stack the admin project table's Team
 * column uses: up to three coloured circles with initials, then a "+N"
 * overflow chip. Hovering a circle names the person.
 */
export const ClientAvatarStack: React.FC<{
  members: { id: number; name: string }[];
}> = ({ members }) => {
  if (members.length === 0) return <span className="text-[#475569]">—</span>;
  return (
    <div className="flex -space-x-2 items-center p-1">
      {members.slice(0, 3).map((member) => (
        <div
          key={member.id}
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ring-2 ring-white text-white text-[10px] font-bold shadow-sm ${AVATAR_COLORS[member.id % AVATAR_COLORS.length]}`}
          title={member.name}
        >
          {(member.name || 'U').split(' ').map((part) => part[0]).join('').substring(0, 2).toUpperCase()}
        </div>
      ))}
      {members.length > 3 && (
        <div
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full ring-2 ring-white bg-slate-100 text-slate-500 text-[10px] font-bold shadow-sm"
          title={members.slice(3).map((member) => member.name).join(', ')}
        >
          +{members.length - 3}
        </div>
      )}
    </div>
  );
};

/**
 * The admin screens' table dressing (see `AdminClients`), shared by the
 * client portal's list pages so a client sees the same Monitra design
 * language an admin does: white rounded card, uppercase slate header row,
 * hairline row dividers.
 */
export const ClientTable: React.FC<{
  headers: { label: string; align?: 'right' }[];
  children: React.ReactNode;
}> = ({ headers, children }) => (
  <div className="overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="bg-[#F8FAFC] text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
          <tr>
            {headers.map((header) => (
              <th
                key={header.label}
                className={`px-4 py-3 ${header.align === 'right' ? 'text-right' : ''}`}
              >
                {header.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[#F1F5F9]">{children}</tbody>
      </table>
    </div>
  </div>
);
