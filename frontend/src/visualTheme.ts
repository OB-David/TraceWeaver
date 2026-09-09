export const THEME_FILL_COLORS = [
  '#F4B9B9',
  '#F3DAB5',
  '#F2EBB5',
  '#D2F3B4',
  '#B4F3BA',
  '#B5F2D3',
  '#B5EBF4',
  '#B6CDF3',
  '#C3B5F2',
  '#EBB8F4',
  '#F3B4DB',
  '#E2E2E2',
] as const

export const THEME_DARK_COLORS = [
  '#C79696',
  '#C9B495',
  '#C3BD90',
  '#ADC893',
  '#8EC293',
  '#89BAA2',
  '#8EBBC1',
  '#91A4C2',
  '#968CBE',
  '#B289B9',
  '#BA85A6',
  '#B6B6B6',
] as const

const LEGACY_CONTRACT_COLORS = [
  '#FD6767E6',
  '#FF956EE6',
  '#FFA500E6',
  '#80A700BC',
  '#21D22DBB',
  '#065700CA',
  '#B36EF985',
  '#1B87F3BB',
  '#87CEFAE6',
  '#ADD8E6E6',
] as const

export const CFG_EDGE_COLORS = {
  NORMAL: '#64748B',
  JUMP: '#6B5B73',
  CALL: '#4D7C61',
  DELEGATECALL: '#4F78A0',
  TERMINATE: '#9A6658',
} as const

const THEME_BY_FILL = new Map<string, { fill: string; dark: string }>()

for (let i = 0; i < THEME_FILL_COLORS.length; i += 1) {
  const fill = THEME_FILL_COLORS[i]
  const dark = THEME_DARK_COLORS[i]
  if (!fill || !dark) continue
  THEME_BY_FILL.set(fill.toLowerCase(), {
    fill,
    dark,
  })
}

for (let i = 0; i < LEGACY_CONTRACT_COLORS.length; i += 1) {
  const legacy = LEGACY_CONTRACT_COLORS[i]
  const fill = THEME_FILL_COLORS[i]
  const dark = THEME_DARK_COLORS[i]
  if (!legacy || !fill || !dark) continue
  THEME_BY_FILL.set(normalizeColor(legacy), {
    fill,
    dark,
  })
}

export function normalizeColor(color: string | null | undefined): string {
  return (color || '').trim().toLowerCase().replace(/[^#a-f0-9]/g, '')
}

export function resolveThemeColor(color: string | null | undefined) {
  const normalized = normalizeColor(color)
  const configured = THEME_BY_FILL.get(normalized)
  if (configured) return configured

  // Newly added palette colors should remain usable without maintaining a
  // second exhaustive lookup table in the frontend. Accept #RRGGBB and
  // #RRGGBBAA, preserving the fill and deriving a darker border/text accent.
  const match = /^#([a-f0-9]{6})(?:[a-f0-9]{2})?$/.exec(normalized)
  if (!match?.[1]) return null

  const hex = match[1]
  const fill = `#${hex.toUpperCase()}`
  const darkChannels = [0, 2, 4].map(offset =>
    Math.round(Number.parseInt(hex.slice(offset, offset + 2), 16) * 0.78),
  )
  const dark = `#${darkChannels.map(channel => channel.toString(16).padStart(2, '0')).join('').toUpperCase()}`
  return { fill, dark }
}

export function getDarkAccentForColor(color: string | null | undefined, fallback = '#6B7280') {
  return resolveThemeColor(color)?.dark || fallback
}

export function getFillColorForColor(color: string | null | undefined, fallback = '#E2E2E2') {
  return resolveThemeColor(color)?.fill || fallback
}

export function getReadableTextOnDark(color: string | null | undefined) {
  const normalized = normalizeColor(color)
  return normalized === '#b6b6b6' ? '#1f2937' : '#ffffff'
}

export function getReadableTextOnLight(
  color: string | null | undefined,
  fallback = '#374151',
) {
  const match = /^#([a-f0-9]{6})(?:[a-f0-9]{2})?$/.exec(normalizeColor(color))
  if (!match?.[1]) return fallback

  const channels = [0, 2, 4].map(offset =>
    Number.parseInt(match[1]!.slice(offset, offset + 2), 16),
  )
  const relativeLuminance = (values: number[]) => {
    const linear = values.map((value) => {
      const channel = value / 255
      return channel <= 0.04045
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * linear[0]! + 0.7152 * linear[1]! + 0.0722 * linear[2]!
  }
  const hasReadableContrast = (values: number[]) => (
    1.05 / (relativeLuminance(values) + 0.05) >= 4.5
  )

  if (hasReadableContrast(channels)) return `#${match[1].toUpperCase()}`

  // Preserve the source hue while finding the lightest darker shade that
  // reaches the normal-text contrast threshold on the white graph canvas.
  let readableScale = 0
  let unreadableScale = 1
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const scale = (readableScale + unreadableScale) / 2
    const candidate = channels.map(channel => Math.round(channel * scale))
    if (hasReadableContrast(candidate)) readableScale = scale
    else unreadableScale = scale
  }
  const readableChannels = channels.map(channel => Math.round(channel * readableScale))
  return `#${readableChannels
    .map(channel => channel.toString(16).padStart(2, '0'))
    .join('')
    .toUpperCase()}`
}
