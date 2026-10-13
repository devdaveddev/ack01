import { useEffect, useRef } from 'react'
import * as THREE from 'three'

// A ring of small shards. Move the pointer over it (or tap on touch screens) and the shards near
// it break away and drift; when the pointer leaves they drift back and the ring reassembles.
const MAJOR = 160 // shards around the ring
const MINOR = 48 // shards around the tube
const RADIUS = 2.2
const TUBE = 0.9
const REACH = 2.9 // how far from the pointer a shard is pushed
const COLORS = ['#2a64b8', '#3d78c8', '#6b8fb8', '#8a97a8', '#b9c4d2'].map((c) => new THREE.Color(c))

const reducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

export default function Ring({ label = 'Interactive ring. Move over it to break it apart.' }) {
  const mount = useRef(null)

  useEffect(() => {
    const host = mount.current
    if (!host) return undefined

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    host.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100)
    camera.position.set(0, 0, 13)

    scene.add(new THREE.AmbientLight(0xffffff, 0.9))
    const key = new THREE.DirectionalLight(0xffffff, 1.4)
    key.position.set(4, 6, 8)
    scene.add(key)

    // One shard per cell of the ring's surface, oriented to the surface.
    const count = MAJOR * MINOR
    const geometry = new THREE.BoxGeometry(0.1, 0.1, 0.18)
    const material = new THREE.MeshStandardMaterial({ roughness: 0.35, metalness: 0.1, emissive: 0x2a64b8, emissiveIntensity: 0.35 })
    const shards = new THREE.InstancedMesh(geometry, material, count)
    const base = []
    const offset = []
    const scatter = []
    const matrix = new THREE.Matrix4()
    const quat = new THREE.Quaternion()
    const pos = new THREE.Vector3()
    const normal = new THREE.Vector3()
    const tangentU = new THREE.Vector3()
    let i = 0
    for (let a = 0; a < MAJOR; a++) {
      for (let b = 0; b < MINOR; b++) {
        const u = (a / MAJOR) * Math.PI * 2
        const v = (b / MINOR) * Math.PI * 2
        const cosV = Math.cos(v)
        pos.set((RADIUS + TUBE * cosV) * Math.cos(u), (RADIUS + TUBE * cosV) * Math.sin(u), TUBE * Math.sin(v))
        normal.set(cosV * Math.cos(u), cosV * Math.sin(u), Math.sin(v)).normalize()
        tangentU.set(-Math.sin(u), Math.cos(u), 0)
        const basis = new THREE.Matrix4().makeBasis(tangentU, normal.clone().cross(tangentU), normal)
        quat.setFromRotationMatrix(basis)
        base.push(pos.clone())
        offset.push({ pos: new THREE.Vector3(), vel: new THREE.Vector3(), spin: new THREE.Vector3() })
        scatter.push(Math.random())
        matrix.compose(pos, quat, new THREE.Vector3(1, 1, 1))
        shards.setMatrixAt(i, matrix)
        shards.setColorAt(i, COLORS[Math.floor(Math.random() * COLORS.length)])
        i++
      }
    }
    shards.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
    scene.add(shards)

    // Invisible plane through the origin that the pointer is projected onto.
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0)
    const ray = new THREE.Raycaster()
    const ndc = new THREE.Vector2(9, 9)
    const hit = new THREE.Vector3()
    let pointer = null // world position of the pointer on the plane, or null when away
    let raf = 0
    let running = !reducedMotion()

    const size = () => {
      const w = host.clientWidth || 1
      const h = host.clientHeight || 1
      renderer.setSize(w, h, false)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    size()
    const resize = new ResizeObserver(size)
    resize.observe(host)

    const place = (clientX, clientY) => {
      const rect = host.getBoundingClientRect()
      ndc.set(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1)
      ray.setFromCamera(ndc, camera)
      pointer = ray.ray.intersectPlane(plane, hit) ? hit.clone() : null
    }
    const onMove = (e) => place(e.clientX, e.clientY)
    const onLeave = () => { pointer = null }
    const onTap = (e) => place(e.clientX, e.clientY)
    host.addEventListener('pointermove', onMove)
    host.addEventListener('pointerleave', onLeave)
    host.addEventListener('pointerdown', onTap)

    const tmp = new THREE.Vector3()
    const goal = new THREE.Vector3()
    const scaleOne = new THREE.Vector3(1, 1, 1)
    const spinGoal = new THREE.Vector3()
    const dq = new THREE.Quaternion()
    const eul = new THREE.Euler()
    const m = new THREE.Matrix4()
    const frame = () => {
      const t = performance.now() / 1000
      for (let k = 0; k < count; k++) {
        const o = offset[k]
        const home = base[k]
        goal.set(0, 0, 0)
        if (pointer) {
          tmp.copy(home).sub(pointer)
          const d = tmp.length()
          if (d < REACH) {
            const s = (1 - d / REACH) ** 2
            // Shards fly out from the pointer in their own directions, so the ring visibly splits.
            goal.copy(tmp.normalize()).multiplyScalar(s * 3.4 * (0.4 + scatter[k]))
            goal.x += s * (scatter[k] - 0.5) * 1.6
            goal.y += s * (0.5 - scatter[k]) * 1.6
            goal.z += s * (scatter[k] - 0.5) * 1.6
          }
        }
        // Pull toward the goal while the pointer is near; drift back otherwise.
        o.vel.lerp(goal.sub(o.pos).multiplyScalar(0.12), 0.25)
        o.pos.add(o.vel).multiplyScalar(0.94)
        spinGoal.set(scatter[k] - 0.5, 0.5 - scatter[k], 0.3).multiplyScalar(pointer ? 0.12 : 0)
        o.spin.lerp(spinGoal, 0.05)
        eul.set(o.spin.x * t, o.spin.y * t, o.spin.z * t)
        dq.setFromEuler(eul)
        tmp.copy(home).add(o.pos)
        m.compose(tmp, dq, scaleOne)
        shards.setMatrixAt(k, m)
      }
      shards.instanceMatrix.needsUpdate = true
      scene.rotation.z = Math.sin(t * 0.15) * 0.08
      scene.rotation.x = 0.55 + Math.cos(t * 0.12) * 0.05
      scene.rotation.y = t * 0.12
      renderer.render(scene, camera)
      if (running) raf = requestAnimationFrame(frame)
    }
    if (reducedMotion()) {
      frame()
    } else {
      raf = requestAnimationFrame(frame)
    }

    return () => {
      running = false
      cancelAnimationFrame(raf)
      resize.disconnect()
      host.removeEventListener('pointermove', onMove)
      host.removeEventListener('pointerleave', onLeave)
      host.removeEventListener('pointerdown', onTap)
      geometry.dispose()
      material.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [])

  return <div ref={mount} className="ring" role="img" aria-label={label} />
}
