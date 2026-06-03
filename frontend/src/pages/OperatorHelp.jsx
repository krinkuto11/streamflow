import { Link } from 'react-router-dom'
import {
  ArrowRight,
  CheckCircle2,
  CircleHelp,
  Cpu,
  Eye,
  Gauge,
  ListChecks,
  RotateCw,
  ShieldCheck,
} from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import { operatorHelpQuickChecks, operatorHelpSections } from '@/lib/operator-help-content.js'

const sectionIcons = {
  'startup-cache': RotateCw,
  'profiles-periods': ListChecks,
  'stream-checker': Gauge,
  'shadow-monitor': Eye,
  hardware: Cpu,
  troubleshooting: ShieldCheck,
}

export default function OperatorHelp() {
  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <CircleHelp className="h-8 w-8 text-primary" />
            <h1 className="text-3xl font-bold tracking-tight">Help</h1>
          </div>
          <p className="max-w-3xl text-muted-foreground">
            Operational notes for the V3 workflows that operators use most often.
          </p>
        </div>
        <Badge variant="outline" className="w-fit px-3 py-1 text-sm">V3 Guide</Badge>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {operatorHelpQuickChecks.map((check) => (
          <div
            key={check}
            className="flex min-h-16 items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm font-medium"
          >
            <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />
            <span>{check}</span>
          </div>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {operatorHelpSections.map((section) => {
          const Icon = sectionIcons[section.id] || CircleHelp

          return (
            <Card key={section.id} className="overflow-hidden">
              <CardHeader className="space-y-2">
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-md border border-border bg-background">
                    <Icon className="h-5 w-5 text-primary" />
                  </div>
                  <div>
                    <CardTitle className="text-xl">{section.title}</CardTitle>
                    <CardDescription>{section.summary}</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <ul className="space-y-2 text-sm text-muted-foreground">
                  {section.items.map((item) => (
                    <li key={item} className="flex gap-2">
                      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
                <div className="flex flex-wrap gap-2">
                  {section.links.map((link) => (
                    <Button key={link.to} asChild variant="outline" size="sm">
                      <Link to={link.to}>
                        {link.label}
                        <ArrowRight className="ml-2 h-3.5 w-3.5" />
                      </Link>
                    </Button>
                  ))}
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
