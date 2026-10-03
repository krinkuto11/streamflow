import { createContext, useContext, useEffect, useState } from 'react'

const ThemeContext = createContext({
  theme: 'auto',
  setTheme: () => {},
  effectiveTheme: 'light'
})

export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState(() => {
    // Load theme from localStorage or default to 'auto'
    return localStorage.getItem('theme') || 'auto'
  })
  
  const [effectiveTheme, setEffectiveTheme] = useState('light')

  useEffect(() => {
    // Save theme to localStorage
    localStorage.setItem('theme', theme)

    // Determine the effective theme
    let effective = theme === 'matrix' ? 'dark' : theme
    if (theme === 'auto') {
      // Check system preference
      effective = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    }

    setEffectiveTheme(effective)

    // Update document class
    const root = document.documentElement
    root.classList.remove('light', 'dark', 'matrix')
    root.classList.add(effective)
    if (theme === 'matrix') root.classList.add('matrix')
  }, [theme])

  // Listen for system theme changes when in auto mode
  useEffect(() => {
    if (theme !== 'auto') return

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = (e) => {
      const effective = e.matches ? 'dark' : 'light'
      setEffectiveTheme(effective)
      const root = document.documentElement
      root.classList.remove('light', 'dark', 'matrix')
      root.classList.add(effective)
    }

    mediaQuery.addEventListener('change', handleChange)
    return () => mediaQuery.removeEventListener('change', handleChange)
  }, [theme])

  return (
    <ThemeContext.Provider value={{ theme, setTheme, effectiveTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return context
}
