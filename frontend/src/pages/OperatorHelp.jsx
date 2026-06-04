import { Link, Navigate, useParams } from 'react-router-dom'
import {
  ArrowRight,
  ArrowLeft,
  CheckCircle2,
  CircleHelp,
  Cpu,
  Eye,
  Gauge,
  ListChecks,
  RotateCw,
  ShieldCheck,
  CalendarCheck,
} from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import {
  getOperatorHelpDetailTopic,
  operatorHelpDetailGuidePrinciples,
  operatorHelpQuickChecks,
  operatorHelpSections,
} from '@/lib/operator-help-content.js'

const sectionIcons = {
  'startup-cache': RotateCw,
  'profiles-periods': ListChecks,
  'stream-checker': Gauge,
  'teamarr-preflight': CalendarCheck,
  'shadow-monitor': Eye,
  hardware: Cpu,
  troubleshooting: ShieldCheck,
  setup: RotateCw,
  'automation-periods': ListChecks,
  'hardware-fallback': Cpu,
}

export default function OperatorHelp() {
  const { topicId } = useParams()
  const topic = topicId ? getOperatorHelpDetailTopic(topicId) : null

  if (topicId && !topic) {
    return <Navigate to="/help" replace />
  }

  if (topic) {
    const Icon = sectionIcons[topic.id] || CircleHelp

    return (
      <div className="space-y-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-2">
            <Button asChild variant="outline" size="sm" className="w-fit">
              <Link to="/help">
                <ArrowLeft className="mr-2 h-3.5 w-3.5" />
                Help Overview
              </Link>
            </Button>
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-md border border-border bg-card">
                <Icon className="h-5 w-5 text-primary" />
              </div>
              <div>
                <h1 className="text-3xl font-bold tracking-tight">{topic.title}</h1>
                <p className="max-w-3xl text-muted-foreground">{topic.summary}</p>
              </div>
            </div>
          </div>
          <Badge variant="outline" className="w-fit px-3 py-1 text-sm">Detailed Guide</Badge>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{topic.visual.title}</CardTitle>
            <CardDescription>Visual flow for the work area</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 md:grid-cols-4">
              {topic.visual.steps.map((step, index) => (
                <div key={step} className="min-h-24 rounded-md border border-border bg-muted/30 p-3">
                  <div className="mb-3 flex items-center justify-between">
                    <Badge variant="secondary">{index + 1}</Badge>
                    {index < topic.visual.steps.length - 1 ? (
                      <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4 text-primary" />
                    )}
                  </div>
                  <p className="text-sm font-medium">{step}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <Card>
            <CardHeader>
              <CardTitle>Step By Step</CardTitle>
              <CardDescription>Use these checks before changing wider automation behavior</CardDescription>
            </CardHeader>
            <CardContent>
              <ol className="space-y-3 text-sm text-muted-foreground">
                {topic.steps.map((step, index) => (
                  <li key={step} className="flex gap-3">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border bg-background text-xs font-semibold text-foreground">
                      {index + 1}
                    </span>
                    <span>{step}</span>
                  </li>
                ))}
              </ol>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Settings And Controls</CardTitle>
              <CardDescription>Where each control lives, what it does, and what to watch</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {topic.settings.map((setting) => (
                <div key={setting.name} className="rounded-md border border-border p-3">
                  <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <h3 className="text-sm font-semibold">{setting.name}</h3>
                    <Badge variant="secondary" className="w-fit max-w-full whitespace-normal text-left">
                      {setting.controlType || 'Visible UI setting'}
                    </Badge>
                  </div>
                  <dl className="grid gap-2 text-sm text-muted-foreground">
                    <div>
                      <dt className="font-medium text-foreground">Where</dt>
                      <dd className="space-y-2">
                        <span>{setting.location}</span>
                        {(setting.locationTo || topic.settingsLocationTo) && (
                          <Button asChild variant="outline" size="sm" className="block w-fit">
                            <Link to={setting.locationTo || topic.settingsLocationTo}>
                              Open Location
                              <ArrowRight className="ml-2 h-3.5 w-3.5" />
                            </Link>
                          </Button>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt className="font-medium text-foreground">Default</dt>
                      <dd>{setting.defaultValue}</dd>
                    </div>
                    <div>
                      <dt className="font-medium text-foreground">Effect</dt>
                      <dd>{setting.effect}</dd>
                    </div>
                    <div>
                      <dt className="font-medium text-foreground">Use When</dt>
                      <dd>{setting.useWhen}</dd>
                    </div>
                    <div>
                      <dt className="font-medium text-foreground">Watch Out</dt>
                      <dd>{setting.risk}</dd>
                    </div>
                  </dl>
                  {setting.reference ? (
                    <details className="mt-3 border-t border-border pt-3">
                      <summary className="cursor-pointer text-sm font-medium text-foreground">
                        Visual Reference
                      </summary>
                      <figure className="mt-3 space-y-2">
                        <img
                          src={setting.reference.imageSrc}
                          alt={setting.reference.alt}
                          loading="lazy"
                          className="max-h-72 w-full rounded-md border border-border object-contain"
                        />
                        <figcaption className="text-xs text-muted-foreground">
                          {setting.reference.caption}
                        </figcaption>
                      </figure>
                    </details>
                  ) : null}
                </div>
              ))}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Smoke Checks</CardTitle>
            <CardDescription>Platform-neutral checks that confirm the setting is doing what you expect</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <ul className="space-y-2 text-sm text-muted-foreground">
              {topic.smokeChecks.map((check) => (
                <li key={check} className="flex gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <span>{check}</span>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap gap-2">
              {topic.links.map((link) => (
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
      </div>
    )
  }

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

      <div className="grid gap-3 lg:grid-cols-2">
        {operatorHelpDetailGuidePrinciples.map((principle) => (
          <div key={principle} className="rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            {principle}
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
