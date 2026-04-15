/**
 * ProfilePickerDialog
 *
 * Shown when a channel has multiple automation periods with different profiles
 * and the user clicks "Health Check". The user selects which profile should
 * govern this one-time check invocation. The selection is NOT persisted.
 *
 * Props:
 *   open          {boolean}   — controls dialog visibility
 *   onOpenChange  {function}  — called with false when dialog should close
 *   channelName   {string}    — displayed in the dialog title
 *   periods       {Array}     — enriched period objects from getChannelPeriods
 *                               each has: { id, name, profile_id, profile_name, schedule }
 *   onSelect      {function}  — called with profileId when user confirms
 */

import { useState, useEffect } from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Label } from '@/components/ui/label.jsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.jsx'
import { Loader2 } from 'lucide-react'

export function ProfilePickerDialog({
  open,
  onOpenChange,
  channelName,
  periods = [],
  onSelect,
}) {
  const [selectedProfileId, setSelectedProfileId] = useState('')

  // Build a deduplicated list of profiles from the periods array.
  // A channel may have N periods that all share the same profile — in that
  // case we show one entry, not N. Keyed on profile_id.
  const uniqueProfiles = (() => {
    const seen = new Map()
    for (const period of periods) {
      if (period.profile_id && !seen.has(period.profile_id)) {
        seen.set(period.profile_id, {
          profile_id: period.profile_id,
          profile_name: period.profile_name || `Profile ${period.profile_id}`,
        })
      }
    }
    return Array.from(seen.values())
  })()

  // Reset selection when dialog opens so stale selection doesn't persist
  useEffect(() => {
    if (open) {
      setSelectedProfileId('')
    }
  }, [open])

  const handleConfirm = () => {
    if (!selectedProfileId) return
    onSelect(selectedProfileId)
    onOpenChange(false)
  }

  const handleCancel = () => {
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>Select Check Profile</DialogTitle>
          <DialogDescription>
            <strong>{channelName}</strong> has multiple automation profiles assigned.
            Select which profile should govern this health check.
            This selection applies to this check only and is not saved.
          </DialogDescription>
        </DialogHeader>

        <div className="py-4 space-y-3">
          <Label htmlFor="profile-select">Profile</Label>
          <Select
            value={selectedProfileId}
            onValueChange={setSelectedProfileId}
          >
            <SelectTrigger id="profile-select">
              <SelectValue placeholder="Choose a profile…" />
            </SelectTrigger>
            <SelectContent>
              {uniqueProfiles.map((p) => (
                <SelectItem key={p.profile_id} value={p.profile_id}>
                  {p.profile_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={handleCancel}>
            Cancel
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={!selectedProfileId}
          >
            Run Health Check
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
