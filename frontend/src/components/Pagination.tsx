import React from 'react';
import { PaginationArrow } from './PaginationArrow';

/**
 * The footer of a paginated list: "Showing 1 to 20 of 66 members", a page-size
 * picker, and numbered pages with ellipses.
 *
 * It was the Members page's own component; it lives here so a second list
 * (Assign Tasks) can wear exactly the same footer instead of a lookalike that
 * drifts. The previous/next arrows are `PaginationArrow`, as everywhere.
 */
export const Pagination: React.FC<{
  page: number;
  totalPages: number;
  totalItems: number;
  limit: number;
  setPage: (page: number) => void;
  setLimit: (limit: number) => void;
  /** What is being counted, in the plural: "members", "projects". */
  noun: string;
  pageSizes?: number[];
  className?: string;
}> = ({ page, totalPages, totalItems, limit, setPage, setLimit, noun, pageSizes = [12, 20, 50, 100], className = 'mt-8' }) => {
  const startItem = totalItems === 0 ? 0 : (page - 1) * limit + 1;
  const endItem = Math.min(page * limit, totalItems);
  const pages: (number | '...')[] = totalPages <= 7
    ? Array.from({ length: totalPages }, (_, index) => index + 1)
    : page <= 4
      ? [1, 2, 3, 4, 5, '...', totalPages]
      : page >= totalPages - 3
        ? [1, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages]
        : [1, '...', page - 1, page, page + 1, '...', totalPages];

  return (
    <div className={`${className} flex flex-col items-center justify-between gap-4 border-t border-slate-200 pt-5 text-sm text-slate-500 sm:flex-row`}>
      <div className="text-center">Showing {startItem} to {endItem} of {totalItems} {noun}</div>
      {/* flex-wrap + centred: on a phone the page-size picker and the pager are
          wider than the screen together, so the picker sits on a row above the
          pager instead of pushing it off the edge. */}
      <div className="flex flex-wrap items-center justify-center gap-3">
        <select
          aria-label="Rows per page"
          value={limit}
          onChange={(e) => { setLimit(Number(e.target.value)); setPage(1); }}
          className="rounded-md border border-slate-300 py-1.5 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          {pageSizes.map((size) => (
            <option key={size} value={size}>{size}</option>
          ))}
        </select>
        {/* Seven page buttons plus two arrows is 292px at w-8/gap-1: more than a
            320px phone has. Slightly narrower buttons below `sm` bring it to 276px. */}
        <div className="flex items-center gap-0.5 sm:gap-1">
          <PaginationArrow direction="prev" disabled={page === 1} onClick={() => setPage(page - 1)} />
          {pages.map((visiblePage, index) => visiblePage === '...' ? (
            <span key={`ellipsis-${index}`} className="flex h-8 w-7 items-center justify-center text-slate-400 sm:w-8">...</span>
          ) : (
            <button
              key={visiblePage}
              type="button"
              onClick={() => setPage(visiblePage)}
              className={`flex h-8 w-7 items-center justify-center rounded text-sm sm:w-8 font-semibold transition ${visiblePage === page ? 'bg-blue-500 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-100'}`}
            >
              {visiblePage}
            </button>
          ))}
          <PaginationArrow direction="next" disabled={page === totalPages} onClick={() => setPage(page + 1)} />
        </div>
      </div>
    </div>
  );
};
