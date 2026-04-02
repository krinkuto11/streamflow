import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog.jsx'
import { CheckCircle, Loader2, ArrowRight } from 'lucide-react'

export function MatchResultsList({ results, loading, onLoadMore, maxHeight = 'max-h-[60vh]' }) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground animate-in fade-in duration-200">
        <Loader2 className="h-4 w-4 animate-spin" />
        Testing pattern...
      </div>
    )
  }

  if (!results) return null

  return (
    <div className="space-y-2 animate-in fade-in slide-in-from-top-2 duration-300">
      <div className="border rounded-md p-3 bg-muted/50 transition-all">
        {results.valid ? (
          <>
            <div className="flex items-center gap-2 text-sm font-medium text-green-600 mb-2 animate-in fade-in duration-200">
              <CheckCircle className="h-4 w-4" />
              Valid pattern - {results.total_matches || results.matches?.length || 0} matches found
            </div>
            {results.matches && results.matches.length > 0 && (
              <div className={`space-y-1 ${maxHeight} overflow-y-auto pr-1`}>
                {results.matches.map((match, idx) => (
                  <div
                    key={idx}
                    className="p-2 border-b last:border-0 rounded-sm hover:bg-muted/50 text-sm animate-in fade-in slide-in-from-left-1 duration-200"
                    style={{ animationDelay: `${Math.min(idx * 20, 300)}ms` }}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex-1 min-w-0">
                        <div className="font-medium truncate text-xs sm:text-sm" title={match.stream_name}>
                          {match.stream_name}
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                          {match.m3u_account_name && (
                            <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-5 font-normal text-muted-foreground border-muted-foreground/30">
                              {match.m3u_account_name}
                            </Badge>
                          )}
                          <Badge
                            variant={match.source === 'tvg_id' ? 'default' : 'secondary'}
                            className={`text-[10px] px-1.5 py-0 h-5 font-medium uppercase ${match.source === 'tvg_id' ? 'bg-blue-600 hover:bg-blue-700' : ''}`}
                          >
                            {match.source === 'tvg_id' ? 'TVG-ID' : 'REGEX'}
                          </Badge>
                          {match.source === 'regex' && match.matched_pattern && (
                            <span className="text-[10px] text-muted-foreground font-mono truncate max-w-[120px] bg-background/50 px-1 rounded border border-border/50" title={match.matched_pattern}>
                              /{match.matched_pattern}/
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {results.has_more && (
              <div className="pt-2">
                <Button
                  variant="ghost"
                  size="sm"
                  className="w-full text-primary hover:bg-primary/10 h-8 text-xs font-medium"
                  onClick={onLoadMore}
                  disabled={loading}
                >
                  {loading ? (
                    <>
                      <Loader2 className="h-3 w-3 mr-2 animate-spin" />
                      Loading matches...
                    </>
                  ) : (
                    <>
                      Load More Matches (Showing {results.matches.length})
                    </>
                  )}
                </Button>
              </div>
            )}

            {!results.has_more && results.total_matches > results.matches.length && (
              <div className="text-center py-2 text-xs text-muted-foreground italic">
                End of results ({results.matches.length} matches shown)
              </div>
            )}
          </>
        ) : (
          <div className="text-sm text-destructive">
            {results.error || 'Invalid pattern'}
          </div>
        )}
      </div>
    </div>
  )
}

export function MatchPreviewDialog({ open, onOpenChange, title, results, loading, onLoadMore }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[700px] max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            Preview stream matching results based on current configuration.
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-hidden py-2">
          <MatchResultsList
            results={results}
            loading={loading}
            onLoadMore={onLoadMore}
          />
        </div>

        <DialogFooter className="mt-auto">
          <Button onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
