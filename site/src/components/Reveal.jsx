import { motion } from 'motion/react'
import { enter } from '../lib/motion.js'

// Scroll reveal. Transform and opacity only, plays once, and MotionConfig
// turns the transform off for people who ask for reduced motion.
export default function Reveal({ as = 'div', delay = 0, className, children, ...rest }) {
  const Tag = motion[as]
  return (
    <Tag
      className={className}
      initial={{ opacity: 0, y: 18 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '0px 0px -10% 0px' }}
      transition={{ ...enter, delay }}
      {...rest}
    >
      {children}
    </Tag>
  )
}
