import { useEffect, useState } from 'react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Badge } from '@/components/ui/badge.jsx'
import { GripVertical } from 'lucide-react'

import { channelsAPI } from '@/services/api.js'
import { getCachedChannelLogoUrl, setCachedChannelLogoUrl } from '@/services/channelCache.js'

export function SortableChannelItem({ channel }) {
  const [logoUrl, setLogoUrl] = useState(null)
  const [logoError, setLogoError] = useState(false)

  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: channel.id })

  useEffect(() => {
    const loadLogo = () => {
      const cachedLogo = getCachedChannelLogoUrl(channel.id)
      if (cachedLogo) {
        setLogoUrl(cachedLogo)
        return
      }

      if (channel.logo_id) {
        const nextLogoUrl = channelsAPI.getLogoCached(channel.logo_id)
        setLogoUrl(nextLogoUrl)
        setCachedChannelLogoUrl(channel.id, nextLogoUrl)
      }
    }
    loadLogo()
  }, [channel.id, channel.logo_id])

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-3 p-4 bg-card border rounded-lg ${isDragging ? 'shadow-lg' : 'shadow-sm'}`}
    >
      <div
        {...attributes}
        {...listeners}
        className="cursor-grab active:cursor-grabbing touch-none"
      >
        <GripVertical className="h-5 w-5 text-muted-foreground" />
      </div>

      <div className="w-20 h-10 flex-shrink-0 bg-muted rounded-md flex items-center justify-center overflow-hidden">
        {logoUrl && !logoError ? (
          <img
            src={logoUrl}
            alt={channel.name}
            className="w-full h-full object-contain"
            onError={() => setLogoError(true)}
          />
        ) : (
          <span className="text-xl font-bold text-muted-foreground">
            {channel.name?.charAt(0) || '?'}
          </span>
        )}
      </div>

      <div className="flex-1 flex items-center gap-4">
        <Badge variant="outline" className="font-mono">
          #{channel.channel_number || 'N/A'}
        </Badge>
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate">{channel.name}</div>
          <div className="text-xs text-muted-foreground">ID: {channel.id}</div>
        </div>
      </div>
    </div>
  )
}
