import React, { useState, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, Legend, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts'
import { api } from '@/services/api.js'
import { useToast } from '@/hooks/use-toast.js'
import { TELEMETRY_DATE_RANGES } from '@/lib/telemetry-retention.js'

const COLORS = ['#0088FE', '#00C49F', '#FFBB28', '#FF8042', '#8884D8', '#82ca9d', '#ffc658'];

export default function StatsDashboard() {
  const [globalData, setGlobalData] = useState([])
  const [providerData, setProviderData] = useState([])
  const [loading, setLoading] = useState(true)
  const [days, setDays] = useState('7')
  const [providerMetric, setProviderMetric] = useState('quality')
  const [channelsList, setChannelsList] = useState([])
  const [selectedChannel, setSelectedChannel] = useState('')
  const [channelHistory, setChannelHistory] = useState([])
  const [loadingHistory, setLoadingHistory] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    fetchData()
  }, [days])

  const fetchData = async () => {
    setLoading(true)
    try {
      const [globalRes, providerRes] = await Promise.all([
        api.get(`/telemetry/global?days=${days}`),
        api.get(`/telemetry/providers?days=${days}`)
      ])
      
      if (globalRes.data.success) {
        // Format timestamp safely
        const formattedGlobal = globalRes.data.data.map(d => ({
          ...d,
          timeLabel: new Date(d.timestamp).toLocaleDateString() + ' ' + new Date(d.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})
        }))
        setGlobalData(formattedGlobal)
      }
      
      if (providerRes.data.success) {
        setProviderData(providerRes.data.data)
      }

      const channelsRes = await api.get('/telemetry/channels/list')
      if (channelsRes.data.success) {
        setChannelsList(channelsRes.data.data)
      }
    } catch (err) {
      console.error(err)
      toast({
        title: "Error fetching telemetry",
        description: err.message,
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (selectedChannel) {
      fetchChannelHistory()
    }
  }, [selectedChannel, days])

  const fetchChannelHistory = async () => {
    setLoadingHistory(true)
    try {
      const res = await api.get(`/telemetry/channels/${selectedChannel}?days=${days}`)
      if (res.data.success) {
        setChannelHistory(res.data.data.map(d => ({
          ...d,
          timeLabel: new Date(d.timestamp).toLocaleDateString() + ' ' + new Date(d.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})
        })))
      }
    } catch (err) {
      console.error(err)
    } finally {
      setLoadingHistory(false)
    }
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-500">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent">
            System Analytics
          </h1>
          <p className="text-muted-foreground mt-1">Telemetry, health, and performance of automation jobs.</p>
        </div>
        
        <div className="flex items-center gap-4">
          <Select value={days} onValueChange={setDays}>
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="Date Range" />
            </SelectTrigger>
            <SelectContent>
              {TELEMETRY_DATE_RANGES.map((range) => (
                <SelectItem key={range.value} value={range.value}>{range.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <Tabs defaultValue="overview" className="w-full">
        <TabsList className="grid w-full grid-cols-3 max-w-md bg-muted/50 p-1">
          <TabsTrigger value="overview" className="data-[state=active]:bg-background data-[state=active]:shadow-sm transition-all">Overview</TabsTrigger>
          <TabsTrigger value="providers" className="data-[state=active]:bg-background data-[state=active]:shadow-sm transition-all">Providers</TabsTrigger>
          <TabsTrigger value="channels" className="data-[state=active]:bg-background data-[state=active]:shadow-sm transition-all">Channels</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-6 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Card className="border-none shadow-md bg-card/50 backdrop-blur-sm">
              <CardHeader>
                <CardTitle>Execution Duration</CardTitle>
                <CardDescription>Automated script runtimes over {days} days</CardDescription>
              </CardHeader>
              <CardContent className="h-[300px]">
                {loading ? (
                  <div className="h-full flex items-center justify-center">Loading...</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={globalData}>
                      <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                      <XAxis dataKey="timeLabel" tick={{fontSize: 12}} />
                      <YAxis label={{ value: 'Seconds', angle: -90, position: 'insideLeft' }} />
                      <RechartsTooltip cursor={{fill: 'transparent', stroke: 'rgba(255,255,255,0.1)'}} contentStyle={{borderRadius: '8px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.1)'}} />
                      <Legend />
                      <Line type="monotone" dataKey="duration_seconds" name="Duration (s)" stroke="#8884d8" strokeWidth={3} activeDot={{ r: 8 }} animationDuration={1000} />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </CardContent>
            </Card>

            <Card className="border-none shadow-md bg-card/50 backdrop-blur-sm">
              <CardHeader>
                <CardTitle>Stream Churn Over Time</CardTitle>
                <CardDescription>Global dead vs total channels processed</CardDescription>
              </CardHeader>
              <CardContent className="h-[300px]">
                {loading ? (
                  <div className="h-full flex items-center justify-center">Loading...</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={globalData}>
                      <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                      <XAxis dataKey="timeLabel" tick={{fontSize: 12}} />
                      <YAxis />
                      <RechartsTooltip contentStyle={{borderRadius: '8px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.1)'}} />
                      <Legend />
                      <Line type="monotone" dataKey="global_dead_count" name="Dead Streams" stroke="#ff4d4f" strokeWidth={2} dot={false} animationDuration={1000} />
                      <Line type="monotone" dataKey="total_streams" name="Streams Processed" stroke="#82ca9d" strokeWidth={2} dot={false} animationDuration={1000} />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="providers" className="mt-6 space-y-6">
          <Card className="border-none shadow-md bg-card/50 backdrop-blur-sm">
            <CardHeader>
              <CardTitle>Provider Health & Availability</CardTitle>
              <CardDescription>Available vs dead streams mapped to provider accounts</CardDescription>
            </CardHeader>
            <CardContent className="h-[400px]">
              {loading ? (
                <div className="h-full flex items-center justify-center">Loading...</div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={providerData} margin={{ top: 20, right: 30, left: 20, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                    <XAxis dataKey="provider_name" />
                    <YAxis />
                    <RechartsTooltip contentStyle={{borderRadius: '8px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.1)'}} />
                    <Legend />
                    <Bar dataKey="total_streams" name="Total Streams" fill="#4f46e5" radius={[4, 4, 0, 0]} animationDuration={1000} />
                    <Bar dataKey="dead_streams" name="Dead Streams" fill="#ef4444" radius={[4, 4, 0, 0]} animationDuration={1000} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>
          
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Card className="border-none shadow-md bg-card/50 backdrop-blur-sm">
              <CardHeader className="flex flex-row items-center justify-between">
                <div>
                  <CardTitle>{providerMetric === 'quality' ? 'Average Quality Score' : 'Resolution Distribution'}</CardTitle>
                  <CardDescription>
                    {providerMetric === 'quality' ? 'Calculated quality index per provider' : 'Count of streams by resolution class'}
                  </CardDescription>
                </div>
                <Tabs value={providerMetric} onValueChange={setProviderMetric} className="w-[180px]">
                  <TabsList className="grid w-full grid-cols-2">
                    <TabsTrigger value="quality">Quality</TabsTrigger>
                    <TabsTrigger value="resolution">Res</TabsTrigger>
                  </TabsList>
                </Tabs>
              </CardHeader>
              <CardContent className="h-[300px]">
                {loading ? (
                  <div className="h-full flex items-center justify-center">Loading...</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart layout="vertical" data={providerData} margin={{ top: 5, right: 30, left: 50, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                      <XAxis type="number" domain={providerMetric === 'quality' ? [0, 1] : [0, 'auto']} />
                      <YAxis dataKey="provider_name" type="category" width={100} />
                      <RechartsTooltip cursor={{fill: 'rgba(255,255,255,0.05)'}} contentStyle={{borderRadius: '8px', border: 'none'}} />
                      {providerMetric === 'quality' ? (
                        <Bar dataKey="avg_quality_score" name="Avg Score" fill="#10b981" radius={[0, 4, 4, 0]} barSize={20} animationDuration={1000} />
                      ) : (
                        <>
                          <Legend />
                          <Bar dataKey="res_2160p" name="2160p (4K)" fill="#a855f7" stackId="res" />
                          <Bar dataKey="res_1080p" name="1080p" fill="#4f46e5" stackId="res" />
                          <Bar dataKey="res_720p" name="720p" fill="#22c55e" stackId="res" />
                          <Bar dataKey="res_576p" name="576p" fill="#eab308" stackId="res" />
                          <Bar dataKey="res_SD" name="SD" fill="#ef4444" stackId="res" />
                        </>
                      )}
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </CardContent>
            </Card>

            <Card className="border-none shadow-md bg-card/50 backdrop-blur-sm">
              <CardHeader>
                <CardTitle>Average Bitrate</CardTitle>
                <CardDescription>Average kbps by provider</CardDescription>
              </CardHeader>
              <CardContent className="h-[300px]">
                {loading ? (
                  <div className="h-full flex items-center justify-center">Loading...</div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart layout="vertical" data={providerData} margin={{ top: 5, right: 30, left: 50, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                      <XAxis type="number" />
                      <YAxis dataKey="provider_name" type="category" width={100} />
                      <RechartsTooltip cursor={{fill: 'rgba(255,255,255,0.05)'}} contentStyle={{borderRadius: '8px', border: 'none'}} />
                      <Bar dataKey="avg_bitrate_kbps" name="Avg Bitrate (kbps)" fill="#3b82f6" radius={[0, 4, 4, 0]} barSize={20} animationDuration={1000} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="channels" className="mt-6">
          <Card className="border-none shadow-md bg-card/50 backdrop-blur-sm">
            <CardHeader className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
              <div>
                <CardTitle>Channel History Matrix</CardTitle>
                <CardDescription>Track specific channel health over past runs.</CardDescription>
              </div>
              <Select value={selectedChannel} onValueChange={setSelectedChannel}>
                <SelectTrigger className="w-[200px]">
                  <SelectValue placeholder="Select Channel" />
                </SelectTrigger>
                <SelectContent>
                  {channelsList.map(c => (
                    <SelectItem key={c.id} value={String(c.id)}>{c.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </CardHeader>
            <CardContent>
              {!selectedChannel ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground bg-muted/20 rounded-lg">
                  <div className="text-center">
                    <div className="inline-block p-4 bg-background rounded-full mb-4 shadow-sm border">
                      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="lucide text-primary"><path d="M4 22h14a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v4"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="m3 15 2 2 4-4"/></svg>
                    </div>
                    <h3 className="text-lg font-medium text-foreground">Specify a Channel ID</h3>
                    <p className="mt-1 text-sm">Select a channel from the list to view detailed history matrices.</p>
                  </div>
                </div>
              ) : loadingHistory ? (
                <div className="h-[300px] flex items-center justify-center">Loading channel history...</div>
              ) : (
                <div className="space-y-6">
                  <div className="h-[300px]">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={channelHistory}>
                        <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                        <XAxis dataKey="timeLabel" tick={{fontSize: 12}} />
                        <YAxis />
                        <RechartsTooltip contentStyle={{borderRadius: '8px', border: 'none'}} />
                        <Legend />
                        <Line type="monotone" dataKey="available_streams" name="Healthy Streams" stroke="#10b981" strokeWidth={2} dot={false} />
                        <Line type="monotone" dataKey="dead_streams" name="Dead Streams" stroke="#ef4444" strokeWidth={2} dot={false} strokeDasharray="5 5" />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>

                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4 bg-muted/20 p-4 rounded-lg">
                    {channelHistory.slice(-1).map((h, i) => (
                      <React.Fragment key={i}>
                        <div className="text-center">
                          <div className="text-sm text-muted-foreground">Status</div>
                          <div className={`text-xl font-bold ${h.offline ? 'text-red-500' : 'text-green-500'}`}>
                            {h.offline ? 'Offline' : 'Online'}
                          </div>
                        </div>
                        <div className="text-center">
                          <div className="text-sm text-muted-foreground">Healthy Streams</div>
                          <div className="text-xl font-bold">{h.available_streams}</div>
                        </div>
                        <div className="text-center">
                          <div className="text-sm text-muted-foreground">Dead Streams</div>
                          <div className="text-xl font-bold text-red-500">{h.dead_streams}</div>
                        </div>
                        <div className="text-center">
                          <div className="text-sm text-muted-foreground">Last Seen</div>
                          <div className="text-xl font-bold text-xs">{h.timeLabel}</div>
                        </div>
                      </React.Fragment>
                    ))}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        
      </Tabs>
    </div>
  )
}
