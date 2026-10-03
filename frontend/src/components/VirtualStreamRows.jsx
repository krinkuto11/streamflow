import { cloneElement, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { visibleRowRange } from '@/lib/virtual-rows.js'

function MeasuredRow({ row, id, report }) {
  const ref = useRef(null)
  useLayoutEffect(() => {
    const element = ref.current
    const measure = () => report(id, element.getBoundingClientRect().height)
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [id, report])
  return cloneElement(row, { ref })
}

export function VirtualStreamRows({ items, renderRow, columns = 6 }) {
  const body = useRef(null)
  const heights = useRef(new Map())
  const [heightVersion, setHeightVersion] = useState(0)
  const [viewport, setViewport] = useState({ top: 0, height: 400 })
  const virtual = items.length >= 100
  const report = useCallback((id, height) => {
    if (height > 0 && heights.current.get(id) !== height) {
      heights.current.set(id, height)
      setHeightVersion(version => version + 1)
    }
  }, [])
  useLayoutEffect(() => {
    const valid = new Set(items.map(item => item.id))
    for (const id of heights.current.keys()) if (!valid.has(id)) heights.current.delete(id)
  }, [items])
  useLayoutEffect(() => {
    if (!virtual) return undefined
    const container = body.current.parentElement.parentElement
    let frame = null
    const measure = () => {
      frame = null
      const header = body.current.parentElement.tHead?.offsetHeight || 0
      setViewport({ top: Math.max(0, container.scrollTop - header), height: container.clientHeight })
    }
    const schedule = () => {
      if (frame === null) frame = requestAnimationFrame(measure)
    }
    measure()
    container.addEventListener('scroll', schedule, { passive: true })
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    observer?.observe(container)
    return () => {
      container.removeEventListener('scroll', schedule)
      observer?.disconnect()
      if (frame !== null) cancelAnimationFrame(frame)
    }
  }, [virtual])
  const offsets = useMemo(() => {
    const result = [0]
    for (const item of items) result.push(result.at(-1) + (heights.current.get(item.id) || 56))
    return result
  }, [items, heightVersion])
  const range = virtual ? visibleRowRange(offsets, viewport.top, viewport.height) : { start: 0, end: items.length, before: 0, after: 0 }
  return <tbody ref={body} className="divide-y">
    {range.before > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: range.before, padding: 0, border: 0 }} /></tr>}
    {items.slice(range.start, range.end).map((item, index) => <MeasuredRow key={item.id} id={item.id} report={report} row={cloneElement(renderRow(item), { 'aria-rowindex': range.start + index + 2 })} />)}
    {range.after > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: range.after, padding: 0, border: 0 }} /></tr>}
  </tbody>
}
