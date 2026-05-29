import React from 'react'
import { AlertCircle, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button.jsx'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'

export class AppErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('StreamFlow UI render error:', error, info)
  }

  render() {
    if (!this.state.error) {
      return this.props.children
    }

    return (
      <div className="flex min-h-[60vh] items-center justify-center p-4">
        <Card className="w-full max-w-xl border-destructive/40">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <AlertCircle className="h-5 w-5 text-destructive" />
              Interface Error
            </CardTitle>
            <CardDescription>
              The page hit a rendering problem while live data was updating.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <pre className="max-h-40 overflow-auto rounded-md border bg-muted p-3 text-xs text-muted-foreground">
              {this.state.error?.message || 'Unknown error'}
            </pre>
            <Button onClick={() => window.location.reload()} className="gap-2">
              <RefreshCw className="h-4 w-4" />
              Reload
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }
}
