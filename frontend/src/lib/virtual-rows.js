export function visibleRowRange(offsets, scrollTop, height, overscan = 6) {
  const count = Math.max(0, offsets.length - 1)
  const locate = value => {
    let low = 0
    let high = count
    while (low < high) {
      const mid = Math.floor((low + high + 1) / 2)
      if (offsets[mid] <= value) low = mid
      else high = mid - 1
    }
    return low
  }
  const top = Math.max(0, scrollTop)
  const start = Math.min(count, Math.max(0, locate(top) - overscan))
  const end = Math.min(count, locate(top + Math.max(0, height)) + 1 + overscan)
  return { start, end, before: offsets[start] || 0, after: (offsets[count] || 0) - (offsets[end] || 0) }
}
