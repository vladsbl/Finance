import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ExpandModal } from './ExpandModal'

// TRANSITION_MS in ExpandModal.tsx -- the component stays mounted this
// long after isOpen goes false so the close animation can play. Tests
// that assert "gone after close" wait at least this long.
const TRANSITION_MS = 200

describe('ExpandModal', () => {
  it('renders nothing when isOpen is false', () => {
    render(
      <ExpandModal isOpen={false} onClose={() => {}} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    expect(screen.queryByText('Contenu')).not.toBeInTheDocument()
  })

  it('renders children and title when isOpen is true', () => {
    render(
      <ExpandModal isOpen={true} onClose={() => {}} title="Mon titre">
        <p>Contenu affiche</p>
      </ExpandModal>,
    )
    expect(screen.getByText('Contenu affiche')).toBeInTheDocument()
    expect(screen.getByText('Mon titre')).toBeInTheDocument()
  })

  it('calls onClose when the close button is clicked', () => {
    const onClose = vi.fn()
    render(
      <ExpandModal isOpen={true} onClose={onClose} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Fermer' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Escape is pressed', () => {
    const onClose = vi.fn()
    render(
      <ExpandModal isOpen={true} onClose={onClose} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when the backdrop is clicked, but not when the panel itself is clicked', () => {
    const onClose = vi.fn()
    render(
      <ExpandModal isOpen={true} onClose={onClose} title="Test">
        <p>Contenu cliquable</p>
      </ExpandModal>,
    )
    // Click inside the content panel first -- must NOT close.
    fireEvent.click(screen.getByText('Contenu cliquable'))
    expect(onClose).not.toHaveBeenCalled()

    // Click the backdrop itself (the dialog role element is the overlay).
    fireEvent.click(screen.getByRole('dialog'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('locks and restores body scroll while open', async () => {
    const { rerender } = render(
      <ExpandModal isOpen={true} onClose={() => {}} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    expect(document.body.style.overflow).toBe('hidden')

    rerender(
      <ExpandModal isOpen={false} onClose={() => {}} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    await waitFor(() => expect(document.body.style.overflow).not.toBe('hidden'))
  })

  it('stays mounted briefly after isOpen flips to false, then unmounts', async () => {
    const { rerender } = render(
      <ExpandModal isOpen={true} onClose={() => {}} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    rerender(
      <ExpandModal isOpen={false} onClose={() => {}} title="Test">
        <p>Contenu</p>
      </ExpandModal>,
    )
    // Still present immediately after the flip -- the exit transition
    // needs the panel to still be in the DOM to animate out.
    expect(screen.getByText('Contenu')).toBeInTheDocument()

    await waitFor(
      () => expect(screen.queryByText('Contenu')).not.toBeInTheDocument(),
      { timeout: TRANSITION_MS + 200 },
    )
  })
})
