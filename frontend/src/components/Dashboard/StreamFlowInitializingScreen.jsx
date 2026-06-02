import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Progress } from '@/components/ui/progress.jsx'
import { Loader2 } from 'lucide-react'

export default function StreamFlowInitializingScreen({ initialization = null }) {
  const progress = Number.isFinite(Number(initialization?.percentage))
    ? Math.max(0, Math.min(100, Number(initialization.percentage)))
    : 0

  return (
    <div className="flex min-h-[calc(100vh-8rem)] items-center justify-center">
      <Card className="w-full max-w-4xl">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-2xl">
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
            Initializing StreamFlow
          </CardTitle>
          <CardDescription>Preparing Dispatcharr cache</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="text-muted-foreground">
              {initialization?.message || 'Loading channels, streams, playlists and profiles'}
            </span>
            <span className="tabular-nums text-muted-foreground">{Math.round(progress)}%</span>
          </div>
          <Progress value={progress} className="h-2" />
          <p className="text-sm text-muted-foreground">
            The dashboard will load automatically when startup is complete.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
