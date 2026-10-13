// Shared motion tokens. Every animation in the app pulls from here so the
// rhythm stays consistent (UI UX Pro Max: motion-consistency).
export const spring = { type: 'spring', stiffness: 520, damping: 26, mass: 0.8 }
export const springSoft = { type: 'spring', stiffness: 220, damping: 24 }
export const enter = { duration: 0.32, ease: [0.2, 0.7, 0.2, 1] }
// Exits run at roughly two thirds of the enter time (exit-faster-than-enter).
export const exit = { duration: 0.12, ease: [0.4, 0, 1, 1] }
// Stagger between siblings, inside the 30 to 50ms range.
export const STAGGER = 0.045
