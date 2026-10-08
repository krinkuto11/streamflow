import { Button } from '@/components/ui/button.jsx'

export function ChannelPagination({ page, totalPages, onPageChange, label }) {
  if (totalPages <= 1) return null
  return (
    <nav aria-label={label} className="flex min-w-0 items-center justify-center gap-2 py-3">
      <Button variant="outline" size="sm" className="hidden min-h-11 sm:inline-flex" onClick={() => onPageChange(1)} disabled={page === 1}>First</Button>
      <Button variant="outline" size="sm" className="min-h-11 shrink-0" onClick={() => onPageChange(page - 1)} disabled={page === 1} aria-label="Previous page">Previous</Button>
      <span className="min-w-0 text-center text-xs tabular-nums text-muted-foreground sm:px-3 sm:text-sm" aria-live="polite">Page {page} of {totalPages}</span>
      <Button variant="outline" size="sm" className="min-h-11 shrink-0" onClick={() => onPageChange(page + 1)} disabled={page === totalPages} aria-label="Next page">Next</Button>
      <Button variant="outline" size="sm" className="hidden min-h-11 sm:inline-flex" onClick={() => onPageChange(totalPages)} disabled={page === totalPages}>Last</Button>
    </nav>
  )
}
