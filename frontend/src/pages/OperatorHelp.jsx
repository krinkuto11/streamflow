import { Link, Navigate, useParams } from 'react-router-dom'
import {
  ArrowRight,
  ArrowLeft,
  CheckCircle2,
  CircleHelp,
  Cpu,
  Eye,
  Gauge,
  Image as ImageIcon,
  ListChecks,
  RotateCw,
  ShieldCheck,
  CalendarCheck,
} from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
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
      <div className="min-w-0 space-y-6 overflow-hidden">
        <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 space-y-2">
            <Button asChild variant="outline" size="sm" className="w-fit">
              <Link to="/help">
                <ArrowLeft className="mr-2 h-3.5 w-3.5" />
                Help Overview
              </Link>
            </Button>
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-border bg-card">
                <Icon className="h-5 w-5 text-primary" />
              </div>
              <div className="min-w-0">
                <h1 className="break-words text-3xl font-bold tracking-tight">{topic.title}</h1>
                <p className="max-w-3xl break-words text-muted-foreground">{topic.summary}</p>
              </div>
            </div>
          </div>
          <Badge variant="outline" className="w-fit px-3 py-1 text-sm">Detailed Guide</Badge>
        </div>

        <Card className="min-w-0 max-w-full overflow-hidden">
          <CardHeader>
            <CardTitle>{topic.visual.title}</CardTitle>
            <CardDescription>Visual flow for the work area</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid min-w-0 gap-3 md:grid-cols-4">
              {topic.visual.steps.map((step, index) => (
                <div key={step} className="min-h-24 min-w-0 rounded-md border border-border bg-muted/30 p-3">
                  <div className="mb-3 flex items-center justify-between">
                    <Badge variant="secondary">{index + 1}</Badge>
                    {index < topic.visual.steps.length - 1 ? (
                      <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4 text-primary" />
                    )}
                  </div>
                  <p className="break-words text-sm font-medium">{step}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <Card className="min-w-0 max-w-full overflow-hidden">
            <CardHeader>
              <CardTitle>Step By Step</CardTitle>
              <CardDescription>Use these checks before changing wider automation behavior</CardDescription>
            </CardHeader>
            <CardContent>
              <ol className="space-y-3 text-sm text-muted-foreground">
                {topic.steps.map((step, index) => (
                  <li key={step} className="flex min-w-0 gap-3">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border bg-background text-xs font-semibold text-foreground">
                      {index + 1}
                    </span>
                    <span className="min-w-0 break-words">{step}</span>
                  </li>
                ))}
              </ol>
            </CardContent>
          </Card>

          <Card className="min-w-0 max-w-full overflow-hidden">
            <CardHeader>
              <CardTitle>Settings And Controls</CardTitle>
              <CardDescription>Where each control lives, what it does, and what to watch</CardDescription>
            </CardHeader>
            <CardContent className="min-w-0 space-y-3">
              {topic.settings.map((setting) => (
                <div key={setting.name} className="min-w-0 rounded-md border border-border p-3">
                  <div className="mb-3 flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <h3 className="min-w-0 break-words text-sm font-semibold">{setting.name}</h3>
                    <Badge variant="secondary" className="w-fit max-w-full whitespace-normal text-left">
                      {setting.controlType || 'Visible UI setting'}
                    </Badge>
                  </div>
                  <dl className="grid min-w-0 gap-2 text-sm text-muted-foreground">
                    <div className="min-w-0">
                      <dt className="font-medium text-foreground">Where</dt>
                      <dd className="min-w-0 space-y-2">
                        <span className="block min-w-0 break-words">{setting.location}</span>
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
                    <div className="min-w-0">
                      <dt className="font-medium text-foreground">Default</dt>
                      <dd className="break-words">{setting.defaultValue}</dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="font-medium text-foreground">Effect</dt>
                      <dd className="break-words">{setting.effect}</dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="font-medium text-foreground">Use When</dt>
                      <dd className="break-words">{setting.useWhen}</dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="font-medium text-foreground">Watch Out</dt>
                      <dd className="break-words">{setting.risk}</dd>
                    </div>
                  </dl>
                  {setting.reference ? (
                    <Accordion type="single" collapsible className="mt-3 border-t border-border pt-1">
                      <AccordionItem value={`${setting.name}-screenshot`} className="border-b-0">
                        <AccordionTrigger className="py-2 text-sm hover:no-underline">
                          <span className="flex items-center gap-2">
                            <ImageIcon className="h-4 w-4 text-primary" />
                            {setting.reference.triggerLabel || 'UI Screenshot'}
                          </span>
                        </AccordionTrigger>
                        <AccordionContent>
                          <figure className="space-y-2 pb-1">
                            <img
                              src={setting.reference.imageSrc}
                              alt={setting.reference.alt}
                              loading="lazy"
                              decoding="async"
                              width={setting.reference.width}
                              height={setting.reference.height}
                              className="max-h-72 w-full rounded-md border border-border bg-background object-contain"
                            />
                            <figcaption className="text-xs text-muted-foreground">
                              {setting.reference.caption}
                            </figcaption>
                          </figure>
                        </AccordionContent>
                      </AccordionItem>
                    </Accordion>
                  ) : null}
                </div>
              ))}
            </CardContent>
          </Card>
        </div>

        <Card className="min-w-0 max-w-full overflow-hidden">
          <CardHeader>
            <CardTitle>Smoke Checks</CardTitle>
            <CardDescription>Platform-neutral checks that confirm the setting is doing what you expect</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <ul className="space-y-2 text-sm text-muted-foreground">
              {topic.smokeChecks.map((check) => (
                <li key={check} className="flex min-w-0 gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <span className="min-w-0 break-words">{check}</span>
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
    <div className="min-w-0 space-y-6 overflow-hidden">
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex min-w-0 items-center gap-3">
            <CircleHelp className="h-8 w-8 text-primary" />
            <h1 className="break-words text-3xl font-bold tracking-tight">Help</h1>
          </div>
          <p className="max-w-3xl break-words text-muted-foreground">
            Operational notes for the StreamFlow workflows that operators use most often.
          </p>
        </div>
        <Badge variant="outline" className="w-fit px-3 py-1 text-sm">Operator Guide</Badge>
      </div>

      <div className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {operatorHelpQuickChecks.map((check) => (
          <div
            key={check}
            className="flex min-h-16 min-w-0 items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm font-medium"
          >
            <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />
            <span className="min-w-0 break-words">{check}</span>
          </div>
        ))}
      </div>

      <div className="grid min-w-0 gap-3 lg:grid-cols-2">
        {operatorHelpDetailGuidePrinciples.map((principle) => (
          <div key={principle} className="min-w-0 break-words rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            {principle}
          </div>
        ))}
      </div>

      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        {operatorHelpSections.map((section) => {
          const Icon = sectionIcons[section.id] || CircleHelp

          return (
            <Card key={section.id} className="min-w-0 max-w-full overflow-hidden">
              <CardHeader className="space-y-2">
                <div className="flex min-w-0 items-center gap-3">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-border bg-background">
                    <Icon className="h-5 w-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <CardTitle className="break-words text-xl">{section.title}</CardTitle>
                    <CardDescription className="break-words">{section.summary}</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <ul className="space-y-2 text-sm text-muted-foreground">
                  {section.items.map((item) => (
                    <li key={item} className="flex min-w-0 gap-2">
                      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                      <span className="min-w-0 break-words">{item}</span>
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
