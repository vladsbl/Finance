import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'

// ExpandModal renders via createPortal(..., document.body) -- portals
// attach directly to a real DOM node outside the usual render container,
// so without an explicit cleanup() after each test they'd leak into the
// next test's document instead of being torn down automatically.
afterEach(cleanup)
