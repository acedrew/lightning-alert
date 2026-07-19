<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import L from 'leaflet'

const API_BASE = '' // relative path since we serve it from FastAPI, or http://localhost:8000 in dev

// State variables
const drawMode = ref(null) // null, 'circle', 'polygon', 'wedge'
const drawHelpText = ref('')
const timeoutHours = ref(2.0)
const checkIntervalMinutes = ref(5)
const activeTab = ref('strikes')
const testingWebhook = ref(false)
const localType = ref(null)
const localCoordinates = ref(null)
const focalLength = ref(50) // default 50mm
const isDraggingWedge = ref(false)
const wedgeOriginLat = ref(0.0)
const wedgeOriginLng = ref(0.0)
const wedgeFocalLat = ref(0.0)
const wedgeFocalLng = ref(0.0)
let wedgeFocalMarker = null // Target/focal marker pin

// Draggable markers for wedge
let wedgeOriginMarker = null
let wedgeFgMarker = null
let wedgeBgMarker = null
let wedgeCenterline = null

const commonFocalLengths = [
  { val: 14, label: '14mm (Ultra Wide)' },
  { val: 16, label: '16mm (Wide)' },
  { val: 20, label: '20mm' },
  { val: 24, label: '24mm (Standard Wide)' },
  { val: 28, label: '28mm' },
  { val: 35, label: '35mm (Classic)' },
  { val: 50, label: '50mm (Normal)' },
  { val: 70, label: '70mm' },
  { val: 85, label: '85mm (Portrait)' },
  { val: 105, label: '105mm' },
  { val: 135, label: '135mm' },
  { val: 200, label: '200mm (Telephoto)' },
  { val: 300, label: '300mm' },
  { val: 400, label: '400mm' },
  { val: 500, label: '500mm' },
  { val: 600, label: '600mm' }
]

const savingConfig = ref(false)
const configStatus = ref({
  xweather_configured: false,
  discord_configured: false,
  xweather_api_key_masked: '',
  discord_webhook_masked: ''
})
const configForm = ref({
  xweather_api_key: '',
  discord_webhook: ''
})

const status = ref({
  active: false,
  start_time: null,
  end_time: null,
  timeout_hours: null,
  type: null,
  coordinates: null,
  time_remaining_seconds: null,
  checks_count: 0,
  detections_count: 0,
  recent_strikes: [],
  logs: []
})

// Leaflet map instance and layers
let map = null
let tileLayer = null
let drawLayer = null // Layer group for drawn shapes
let strikeGroup = null // Layer group for detected strikes

// Temporary draw objects
let tempPoints = []
let tempMarkers = []
let tempShape = null
let tempGuideLine = null

// Stored references for strikes
const strikeMarkers = {}

// Computeds
const isConfigured = computed(() => {
  return configStatus.value.xweather_configured && configStatus.value.discord_configured
})

const hasDrawnShape = computed(() => {
  return localCoordinates.value !== null
})

const calculatedFovAngleText = computed(() => {
  const fov = 2 * Math.atan(18 / focalLength.value) * 180 / Math.PI
  return fov.toFixed(1)
})

const estimatedApiCalls = computed(() => {
  const callsPerHour = 60 / checkIntervalMinutes.value
  return Math.round(timeoutHours.value * callsPerHour)
})

const freeTierPercentage = computed(() => {
  const calls = estimatedApiCalls.value
  return ((calls / 1500) * 100).toFixed(2)
})

const shapeDetailsText = computed(() => {
  if (!localCoordinates.value) return ''
  if (localType.value === 'circle') {
    const radiusKm = localCoordinates.value.radius / 1000.0
    return `Circle: ${radiusKm.toFixed(2)} km radius`
  } else if (localType.value === 'wedge') {
    const fgKm = localCoordinates.value.foreground_radius / 1000.0
    const bgKm = localCoordinates.value.background_radius / 1000.0
    const heading = localCoordinates.value.heading.toFixed(1)
    const focal = localCoordinates.value.focal_length
    return `Wedge: ${focal}mm | Heading ${heading}° | Range ${fgKm.toFixed(2)}-${bgKm.toFixed(2)} km`
  } else {
    const pointsCount = localCoordinates.value.polygon?.length || 0
    return `Polygon: ${pointsCount} vertices`
  }
})

const timeRemainingText = computed(() => {
  const seconds = status.value.time_remaining_seconds
  if (seconds === null || seconds === undefined) return '--:--'
  if (seconds <= 0) return 'Expired'
  const hrs = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  const secs = Math.floor(seconds % 60)
  return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
})

const sortedStrikes = computed(() => {
  // Sort latest strikes first
  return status.value.recent_strikes.slice().reverse()
})

let wasActive = false

const fetchStatus = async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/status`)
    if (resp.ok) {
      const data = await resp.json()
      
      const transitionToInactive = wasActive && !data.active
      wasActive = data.active
      
      status.value = data
      
      // Sync local shape with backend when active, initial load, or just stopped
      if (data.active || !localCoordinates.value || transitionToInactive) {
        if (data.type) {
          localType.value = data.type
        }
        if (data.coordinates) {
          localCoordinates.value = data.coordinates
          if (data.type === 'wedge' && data.coordinates.focal_length) {
            focalLength.value = data.coordinates.focal_length
          }
        }
        if (data.interval_minutes) {
          checkIntervalMinutes.value = data.interval_minutes
        }
      }
      
      // Update map features based on status
      updateMapFromStatus()
    }
  } catch (err) {
    console.error('Failed to fetch status:', err)
  }
}

// Map initialization
onMounted(() => {
  // Setup Leaflet map
  map = L.map('map', {
    zoomControl: false
  }).setView([39.8283, -98.5795], 4) // Center of USA default fallback

  // Custom Zoom Control on topright
  L.control.zoom({ position: 'topright' }).addTo(map)

  // OpenStreetMap standard tile layer (inverted via CSS for high-contrast visible road networks)
  tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    subdomains: 'abc',
    maxZoom: 19
  }).addTo(map)

  drawLayer = L.layerGroup().addTo(map)
  strikeGroup = L.featureGroup().addTo(map)

  // Listen to clicks on map for drawing
  map.on('click', handleMapClick)
  map.on('mousemove', handleMapMouseMove)

  // Try browser geolocation to center on user location
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (position) => {
        // Only center on user if backend monitoring is not active
        if (!status.value.active) {
          const lat = position.coords.latitude
          const lon = position.coords.longitude
          map.setView([lat, lon], 10)
          
          // Place a clean location marker at their coordinates
          L.circleMarker([lat, lon], {
            radius: 6,
            color: 'var(--ace-secondary)',
            fillColor: 'var(--ace-secondary)',
            fillOpacity: 1.0,
            weight: 2
          }).addTo(map).bindPopup('You are here')
        }
      },
      (error) => {
        console.warn('Geolocation failed or permission denied:', error)
      }
    )
  }

  // Start polling status
  fetchConfig()
  fetchStatus()
  const interval = setInterval(fetchStatus, 3000)
  onUnmounted(() => {
    clearInterval(interval)
  })
})

const handleMapClick = (e) => {
  if (drawMode.value === 'circle') {
    handleCircleDrawClick(e)
  } else if (drawMode.value === 'polygon') {
    handlePolygonDrawClick(e)
  } else if (drawMode.value === 'wedge') {
    handleWedgeDrawClick(e)
  } else if (drawMode.value === 'wedge_origin') {
    const lat = e.latlng.lat
    const lon = e.latlng.lng
    if (localType.value === 'wedge' && localCoordinates.value) {
      const coords = localCoordinates.value
      const latDiff = lat - coords.origin[0]
      const lngDiff = lon - coords.origin[1]
      coords.origin = [lat, lon]
      if (coords.focal_point) {
        coords.focal_point = [
          coords.focal_point[0] + latDiff,
          coords.focal_point[1] + lngDiff
        ]
      }
      finalizeDraw()
    } else {
      initializeWedge(lat, lon)
    }
  } else if (drawMode.value === 'wedge_focal') {
    const lat = e.latlng.lat
    const lon = e.latlng.lng
    if (localType.value === 'wedge' && localCoordinates.value) {
      const coords = localCoordinates.value
      coords.focal_point = [lat, lon]
      const { bearing } = getDistanceAndBearing(
        coords.origin[0], coords.origin[1],
        coords.focal_point[0], coords.focal_point[1]
      )
      coords.heading = bearing
      finalizeDraw()
    }
  }
}

const handleMapMouseMove = (e) => {
  if (drawMode.value === 'circle' && tempPoints.length === 1) {
    const center = L.latLng(tempPoints[0])
    const currentPoint = e.latlng
    const radius = center.distanceTo(currentPoint)
    
    if (tempShape) {
      tempShape.setRadius(radius)
    } else {
      tempShape = L.circle(center, {
        radius: radius,
        color: 'var(--ace-secondary)',
        dashArray: '5, 5',
        fillColor: 'var(--ace-secondary)',
        fillOpacity: 0.1,
        weight: 2
      }).addTo(drawLayer)
    }
    
    const radiusKm = radius / 1000.0
    drawHelpText.value = `Radius: ${radiusKm.toFixed(2)} km. Click again to set.`
  } else if (drawMode.value === 'polygon' && tempPoints.length > 0) {
    const lastPoint = tempPoints[tempPoints.length - 1]
    const currentPoint = [e.latlng.lat, e.latlng.lng]
    
    if (tempGuideLine) {
      tempGuideLine.setLatLngs([lastPoint, currentPoint])
    } else {
      tempGuideLine = L.polyline([lastPoint, currentPoint], {
        color: 'var(--ace-secondary)',
        dashArray: '5, 5',
        weight: 2
      }).addTo(drawLayer)
    }
  }
}

// Circle drawing logic
const startDrawingCircle = () => {
  cancelDrawing()
  drawMode.value = 'circle'
  drawHelpText.value = 'Click on the map to set the center of the detection circle.'
  tempPoints = []
}

const handleCircleDrawClick = (e) => {
  if (tempPoints.length === 0) {
    // Set center
    const center = [e.latlng.lat, e.latlng.lng]
    tempPoints.push(center)
    
    // Add center marker
    const centerMarker = L.marker(e.latlng, {
      icon: L.divIcon({
        className: 'custom-draw-marker',
        html: '<div style="background: var(--ace-secondary); width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 0 4px rgba(0,0,0,0.5);"></div>',
        iconSize: [12, 12],
        iconAnchor: [6, 6]
      })
    }).addTo(drawLayer)
    
    tempMarkers.push(centerMarker)
    drawHelpText.value = 'Move the mouse and click to set the radius.'
  } else if (tempPoints.length === 1) {
    // Set radius
    const center = L.latLng(tempPoints[0])
    const radius = center.distanceTo(e.latlng)
    
    // Save to local shape coordinates
    localType.value = 'circle'
    localCoordinates.value = {
      center: tempPoints[0],
      radius: radius
    }
    
    finalizeDraw()
  }
}

// Polygon drawing logic
const startDrawingPolygon = () => {
  cancelDrawing()
  drawMode.value = 'polygon'
  drawHelpText.value = 'Click on the map to place vertices of the polygon. Place at least 3 points.'
  tempPoints = []
}

const handlePolygonDrawClick = (e) => {
  const currentPoint = [e.latlng.lat, e.latlng.lng]
  tempPoints.push(currentPoint)
  
  // Add marker for vertex
  const vertexMarker = L.marker(e.latlng, {
    icon: L.divIcon({
      className: 'custom-draw-marker',
      html: `<div style="background: ${tempPoints.length === 1 ? 'var(--ace-primary)' : 'var(--ace-secondary)'}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 0 4px rgba(0,0,0,0.5);"></div>`,
      iconSize: [12, 12],
      iconAnchor: [6, 6]
    })
  }).addTo(drawLayer)
  
  // Click first marker to complete polygon
  if (tempPoints.length === 1) {
    vertexMarker.on('click', (ev) => {
      L.DomEvent.stopPropagation(ev)
      completePolygon()
    })
  }
  
  tempMarkers.push(vertexMarker)
  
  // Update polyline
  if (tempShape) {
    tempShape.setLatLngs(tempPoints)
  } else {
    tempShape = L.polyline(tempPoints, {
      color: 'var(--ace-secondary)',
      weight: 2
    }).addTo(drawLayer)
  }
  
  if (tempPoints.length >= 3) {
    drawHelpText.value = `Placed ${tempPoints.length} points. Click the first point (lime) or click complete to finish.`
  } else {
    drawHelpText.value = `Placed ${tempPoints.length} point(s). Click map for next point.`
  }
}

const completePolygon = () => {
  if (tempPoints.length < 3) {
    alert('Please place at least 3 points to complete the polygon.')
    return
  }
  
  localType.value = 'polygon'
  localCoordinates.value = {
    polygon: tempPoints
  }
  
  finalizeDraw()
}

// Finish draw operations
const finalizeDraw = () => {
  if (tempGuideLine) {
    drawLayer.removeLayer(tempGuideLine)
    tempGuideLine = null
  }
  tempMarkers.forEach(m => drawLayer.removeLayer(m))
  tempMarkers = []
  if (tempShape) {
    drawLayer.removeLayer(tempShape)
    tempShape = null
  }
  
  drawMode.value = null
  drawHelpText.value = ''
  
  updateMapFromStatus()
}

const cancelDrawing = () => {
  drawMode.value = null
  drawHelpText.value = ''
  tempPoints = []
  
  tempMarkers.forEach(m => drawLayer.removeLayer(m))
  tempMarkers = []
  
  if (tempShape) {
    drawLayer.removeLayer(tempShape)
    tempShape = null
  }
  
  if (tempGuideLine) {
    drawLayer.removeLayer(tempGuideLine)
    tempGuideLine = null
  }
  
  clearWedgeHandles()
}

const clearDrawnShape = () => {
  cancelDrawing()
  clearWedgeHandles()
  localType.value = null
  localCoordinates.value = null
  drawLayer.clearLayers()
  if (status.value.active) {
    stopMonitoring()
  }
}

// Wedge Specific Geodesic Helpers & Drawing Methods
const getDestinationPoint = (lat, lng, bearing, distance) => {
  const R = 6371000 // Earth's radius in meters
  const latRad = (lat * Math.PI) / 180
  const lngRad = (lng * Math.PI) / 180
  const bearingRad = (bearing * Math.PI) / 180
  const dByR = distance / R

  const destLatRad = Math.asin(
    Math.sin(latRad) * Math.cos(dByR) +
    Math.cos(latRad) * Math.sin(dByR) * Math.cos(bearingRad)
  )
  const destLngRad = lngRad + Math.atan2(
    Math.sin(bearingRad) * Math.sin(dByR) * Math.cos(latRad),
    Math.cos(dByR) - Math.sin(latRad) * Math.sin(destLatRad)
  )

  return [
    (destLatRad * 180) / Math.PI,
    (destLngRad * 180) / Math.PI
  ]
}

const getDistanceAndBearing = (lat1, lng1, lat2, lng2) => {
  const R = 6371000
  const phi1 = (lat1 * Math.PI) / 180
  const phi2 = (lat2 * Math.PI) / 180
  const deltaPhi = ((lat2 - lat1) * Math.PI) / 180
  const deltaLambda = ((lng2 - lng1) * Math.PI) / 180

  const a = Math.sin(deltaPhi / 2) ** 2 +
            Math.cos(phi1) * Math.cos(phi2) *
            Math.sin(deltaLambda / 2) ** 2
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))
  const distance = R * c

  const y = Math.sin(deltaLambda) * Math.cos(phi2)
  const x = Math.cos(phi1) * Math.sin(phi2) -
            Math.sin(phi1) * Math.cos(phi2) * Math.cos(deltaLambda)
  const bearing = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360

  return { distance, bearing }
}

const getWedgePolygonPoints = (lat, lng, heading, fovAngle, fgRad, bgRad) => {
  const points = []
  const steps = 30
  const halfFov = fovAngle / 2

  // Outer arc
  for (let i = 0; i <= steps; i++) {
    const angle = heading - halfFov + (fovAngle * i) / steps
    points.push(getDestinationPoint(lat, lng, angle, bgRad))
  }

  // Inner arc/point
  if (fgRad > 0) {
    for (let i = steps; i >= 0; i--) {
      const angle = heading - halfFov + (fovAngle * i) / steps
      points.push(getDestinationPoint(lat, lng, angle, fgRad))
    }
  } else {
    points.push([lat, lng])
  }

  return points
}

const clearWedgeHandles = () => {
  if (wedgeOriginMarker) {
    drawLayer.removeLayer(wedgeOriginMarker)
    wedgeOriginMarker = null
  }
  if (wedgeFgMarker) {
    drawLayer.removeLayer(wedgeFgMarker)
    wedgeFgMarker = null
  }
  if (wedgeBgMarker) {
    drawLayer.removeLayer(wedgeBgMarker)
    wedgeBgMarker = null
  }
  if (wedgeFocalMarker) {
    drawLayer.removeLayer(wedgeFocalMarker)
    wedgeFocalMarker = null
  }
  if (wedgeCenterline) {
    drawLayer.removeLayer(wedgeCenterline)
    wedgeCenterline = null
  }
}

const updateWedgeHandles = () => {
  if (localType.value !== 'wedge' || !localCoordinates.value || status.value.active) {
    clearWedgeHandles()
    return
  }

  if (isDraggingWedge.value) {
    return
  }

  const coords = localCoordinates.value
  const origin = coords.origin
  const heading = coords.heading
  const fgRad = coords.foreground_radius
  const bgRad = coords.background_radius

  // Setup default focal point if missing
  if (!coords.focal_point) {
    coords.focal_point = getDestinationPoint(origin[0], origin[1], heading, bgRad / 2)
  }
  const focalPos = coords.focal_point

  const fgPos = getDestinationPoint(origin[0], origin[1], heading, fgRad)
  const bgPos = getDestinationPoint(origin[0], origin[1], heading, bgRad)

  // 1. Origin Marker (Translates the entire wedge)
  if (!wedgeOriginMarker) {
    wedgeOriginMarker = L.marker(origin, {
      draggable: true,
      icon: L.divIcon({
        className: 'custom-wedge-handle',
        html: '<div class="wedge-handle-origin" title="Drag to move camera location">📷</div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
      })
    }).addTo(drawLayer)

    wedgeOriginMarker.on('drag', (e) => {
      isDraggingWedge.value = true
      const newLatlng = e.target.getLatLng()
      const latDiff = newLatlng.lat - coords.origin[0]
      const lngDiff = newLatlng.lng - coords.origin[1]
      
      coords.origin = [newLatlng.lat, newLatlng.lng]
      if (coords.focal_point) {
        coords.focal_point = [
          coords.focal_point[0] + latDiff,
          coords.focal_point[1] + lngDiff
        ]
      }
      
      // Update coordinates inputs refs
      wedgeOriginLat.value = Number(coords.origin[0].toFixed(6))
      wedgeOriginLng.value = Number(coords.origin[1].toFixed(6))
      if (coords.focal_point) {
        wedgeFocalLat.value = Number(coords.focal_point[0].toFixed(6))
        wedgeFocalLng.value = Number(coords.focal_point[1].toFixed(6))
      }
      
      const pts = getWedgePolygonPoints(
        coords.origin[0], coords.origin[1],
        coords.heading, coords.fov_angle,
        coords.foreground_radius, coords.background_radius
      )
      if (renderedActiveShape) {
        renderedActiveShape.setLatLngs(pts)
      }
      
      const newFgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.foreground_radius)
      const newBgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.background_radius)
      
      if (wedgeFgMarker) wedgeFgMarker.setLatLng(newFgPos)
      if (wedgeBgMarker) wedgeBgMarker.setLatLng(newBgPos)
      if (wedgeFocalMarker) wedgeFocalMarker.setLatLng(coords.focal_point)
      if (wedgeCenterline) wedgeCenterline.setLatLngs([coords.origin, newBgPos])
    })

    wedgeOriginMarker.on('dragend', () => {
      isDraggingWedge.value = false
    })
  } else {
    wedgeOriginMarker.setLatLng(origin)
  }

  // 2. Focal Point Marker (Rotates/re-headings the lens view)
  if (!wedgeFocalMarker) {
    wedgeFocalMarker = L.marker(focalPos, {
      draggable: true,
      icon: L.divIcon({
        className: 'custom-wedge-handle',
        html: '<div class="wedge-handle-focal" title="Drag to rotate wedge / point lens">🎯</div>',
        iconSize: [20, 20],
        iconAnchor: [10, 10]
      })
    }).addTo(drawLayer)

    wedgeFocalMarker.on('drag', (e) => {
      isDraggingWedge.value = true
      const dragLatlng = e.target.getLatLng()
      coords.focal_point = [dragLatlng.lat, dragLatlng.lng]
      
      wedgeFocalLat.value = Number(dragLatlng.lat.toFixed(6))
      wedgeFocalLng.value = Number(dragLatlng.lng.toFixed(6))

      const { bearing } = getDistanceAndBearing(
        coords.origin[0], coords.origin[1],
        dragLatlng.lat, dragLatlng.lng
      )
      coords.heading = bearing

      const pts = getWedgePolygonPoints(
        coords.origin[0], coords.origin[1],
        coords.heading, coords.fov_angle,
        coords.foreground_radius, coords.background_radius
      )
      if (renderedActiveShape) {
        renderedActiveShape.setLatLngs(pts)
      }

      // Rotate other centerline markers
      const newFgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.foreground_radius)
      if (wedgeFgMarker) wedgeFgMarker.setLatLng(newFgPos)
      
      const newBgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.background_radius)
      if (wedgeBgMarker) wedgeBgMarker.setLatLng(newBgPos)
      
      if (wedgeCenterline) wedgeCenterline.setLatLngs([coords.origin, newBgPos])
    })

    wedgeFocalMarker.on('dragend', () => {
      isDraggingWedge.value = false
    })
  } else {
    wedgeFocalMarker.setLatLng(focalPos)
  }

  // 3. Foreground Marker (Inner Radius controller)
  if (!wedgeFgMarker) {
    wedgeFgMarker = L.marker(fgPos, {
      draggable: true,
      icon: L.divIcon({
        className: 'custom-wedge-handle',
        html: '<div class="wedge-handle-fg" title="Drag to adjust inner radius"></div>',
        iconSize: [14, 14],
        iconAnchor: [7, 7]
      })
    }).addTo(drawLayer)

    wedgeFgMarker.on('drag', (e) => {
      isDraggingWedge.value = true
      const dragLatlng = e.target.getLatLng()
      const { distance } = getDistanceAndBearing(
        coords.origin[0], coords.origin[1],
        dragLatlng.lat, dragLatlng.lng
      )
      
      const newFgRad = Math.max(0, Math.min(distance, coords.background_radius - 100))
      coords.foreground_radius = newFgRad
      
      const pts = getWedgePolygonPoints(
        coords.origin[0], coords.origin[1],
        coords.heading, coords.fov_angle,
        coords.foreground_radius, coords.background_radius
      )
      if (renderedActiveShape) {
        renderedActiveShape.setLatLngs(pts)
      }
      
      const newFgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.foreground_radius)
      wedgeFgMarker.setLatLng(newFgPos)
    })

    wedgeFgMarker.on('dragend', () => {
      isDraggingWedge.value = false
      const newFgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.foreground_radius)
      if (wedgeFgMarker) wedgeFgMarker.setLatLng(newFgPos)
    })
  } else {
    wedgeFgMarker.setLatLng(fgPos)
  }

  // 4. Background Marker (Outer Radius controller)
  if (!wedgeBgMarker) {
    wedgeBgMarker = L.marker(bgPos, {
      draggable: true,
      icon: L.divIcon({
        className: 'custom-wedge-handle',
        html: '<div class="wedge-handle-bg" title="Drag to adjust outer radius"></div>',
        iconSize: [16, 16],
        iconAnchor: [8, 8]
      })
    }).addTo(drawLayer)

    wedgeBgMarker.on('drag', (e) => {
      isDraggingWedge.value = true
      const dragLatlng = e.target.getLatLng()
      const { distance } = getDistanceAndBearing(
        coords.origin[0], coords.origin[1],
        dragLatlng.lat, dragLatlng.lng
      )
      
      const newBgRad = Math.max(coords.foreground_radius + 100, Math.min(distance, 100000))
      coords.background_radius = newBgRad
      
      const pts = getWedgePolygonPoints(
        coords.origin[0], coords.origin[1],
        coords.heading, coords.fov_angle,
        coords.foreground_radius, coords.background_radius
      )
      if (renderedActiveShape) {
        renderedActiveShape.setLatLngs(pts)
      }
      
      const newBgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.background_radius)
      wedgeBgMarker.setLatLng(newBgPos)
      
      if (wedgeCenterline) wedgeCenterline.setLatLngs([coords.origin, newBgPos])
    })

    wedgeBgMarker.on('dragend', () => {
      isDraggingWedge.value = false
      const newBgPos = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.background_radius)
      if (wedgeBgMarker) wedgeBgMarker.setLatLng(newBgPos)
    })
  } else {
    wedgeBgMarker.setLatLng(bgPos)
  }

  // 5. Centerline
  if (!wedgeCenterline) {
    wedgeCenterline = L.polyline([origin, bgPos], {
      color: 'var(--ace-border-strong)',
      dashArray: '4, 6',
      weight: 2,
      interactive: false
    }).addTo(drawLayer)
  } else {
    wedgeCenterline.setLatLngs([origin, bgPos])
  }
}

// Wedge drawing setup
const startDrawingWedge = () => {
  cancelDrawing()
  drawMode.value = 'wedge'
  drawHelpText.value = 'Click on the map to place the camera location pin, or use the button below to use your current location.'
  tempPoints = []
}

const placeWedgeAtCurrentLocation = () => {
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const lat = position.coords.latitude
        const lon = position.coords.longitude
        initializeWedge(lat, lon)
      },
      (error) => {
        alert('Geolocation failed: ' + error.message)
      }
    )
  } else {
    alert('Geolocation is not supported by your browser.')
  }
}

const handleWedgeDrawClick = (e) => {
  const lat = e.latlng.lat
  const lon = e.latlng.lng
  initializeWedge(lat, lon)
}

const initializeWedge = (lat, lon) => {
  localType.value = 'wedge'
  const fovAngle = 2 * Math.atan(18 / focalLength.value) * 180 / Math.PI
  const heading = 0.0
  const bgRad = 10000.0
  const fp = getDestinationPoint(lat, lon, heading, bgRad / 2)
  localCoordinates.value = {
    origin: [lat, lon],
    heading: heading,
    fov_angle: fovAngle,
    focal_length: focalLength.value,
    foreground_radius: 1000.0,
    background_radius: bgRad,
    focal_point: fp
  }
  map.panTo([lat, lon])
  finalizeDraw()
}

const syncWedgeInputRefs = () => {
  if (localType.value === 'wedge' && localCoordinates.value && !isDraggingWedge.value) {
    const coords = localCoordinates.value
    wedgeOriginLat.value = Number(coords.origin[0].toFixed(6))
    wedgeOriginLng.value = Number(coords.origin[1].toFixed(6))
    
    if (coords.focal_point) {
      wedgeFocalLat.value = Number(coords.focal_point[0].toFixed(6))
      wedgeFocalLng.value = Number(coords.focal_point[1].toFixed(6))
    } else {
      const fp = getDestinationPoint(coords.origin[0], coords.origin[1], coords.heading, coords.background_radius / 2)
      coords.focal_point = fp
      wedgeFocalLat.value = Number(fp[0].toFixed(6))
      wedgeFocalLng.value = Number(fp[1].toFixed(6))
    }
  }
}

const updateWedgeFromInputs = () => {
  if (localType.value === 'wedge' && localCoordinates.value) {
    const coords = localCoordinates.value
    coords.origin = [wedgeOriginLat.value, wedgeOriginLng.value]
    coords.focal_point = [wedgeFocalLat.value, wedgeFocalLng.value]
    
    // Recalculate heading
    const { bearing } = getDistanceAndBearing(
      coords.origin[0], coords.origin[1],
      coords.focal_point[0], coords.focal_point[1]
    )
    coords.heading = bearing
    
    updateMapFromStatus()
  }
}

const pickWedgeOriginOnMap = () => {
  cancelDrawing()
  drawMode.value = 'wedge_origin'
  drawHelpText.value = 'Click on the map to set the camera origin (location pin).'
}

const pickWedgeFocalOnMap = () => {
  cancelDrawing()
  drawMode.value = 'wedge_focal'
  drawHelpText.value = 'Click on the map to set the focal target point.'
}

// Watcher for focalLength
watch(focalLength, (newVal) => {
  if (localType.value === 'wedge' && localCoordinates.value) {
    localCoordinates.value.focal_length = newVal
    const fovAngle = 2 * Math.atan(18 / newVal) * 180 / Math.PI
    localCoordinates.value.fov_angle = fovAngle
    
    // Update focal point if heading changed (not changed here, but redraw wedge)
    updateMapFromStatus()
  }
})

// Style helper to age-fade strike markers
const getMarkerStyleForStrike = (strike) => {
  const strikeTime = new Date(strike.dateTimeISO).getTime()
  const now = Date.now()
  const ageMinutes = (now - strikeTime) / 60000
  
  const isInside = strike.is_inside !== false
  
  if (isInside) {
    if (ageMinutes <= 1) {
      // Active Front (0-1m): vibrant danger red with high opacity
      return {
        radius: 9,
        color: 'var(--ace-danger)',
        fillColor: 'var(--ace-danger)',
        fillOpacity: 0.9,
        weight: 2
      }
    } else if (ageMinutes <= 15) {
      // Recent (1-15m): warm orange, medium opacity
      return {
        radius: 7,
        color: '#f69d1d',
        fillColor: '#f69d1d',
        fillOpacity: 0.65,
        weight: 1.5
      }
    } else if (ageMinutes <= 30) {
      // Intermediate (15-30m): dark/faded orange, lower opacity
      return {
        radius: 6,
        color: '#b06b0d',
        fillColor: '#b06b0d',
        fillOpacity: 0.45,
        weight: 1
      }
    } else {
      // Historical (>30m): small, faded gray, low opacity
      return {
        radius: 5,
        color: 'var(--ace-text-subtle)',
        fillColor: 'var(--ace-text-subtle)',
        fillOpacity: 0.25,
        weight: 1
      }
    }
  } else {
    // Outside the target area (Buffer strikes)
    if (ageMinutes <= 1) {
      return {
        radius: 8,
        color: 'var(--ace-secondary)',
        fillColor: 'var(--ace-secondary)',
        fillOpacity: 0.8,
        weight: 1.5
      }
    } else if (ageMinutes <= 15) {
      return {
        radius: 6,
        color: 'var(--ace-secondary)',
        fillColor: 'var(--ace-secondary)',
        fillOpacity: 0.5,
        weight: 1
      }
    } else if (ageMinutes <= 30) {
      return {
        radius: 5,
        color: 'var(--ace-secondary)',
        fillColor: 'var(--ace-secondary)',
        fillOpacity: 0.3,
        weight: 0.8
      }
    } else {
      return {
        radius: 4,
        color: 'var(--ace-text-subtle)',
        fillColor: 'var(--ace-text-subtle)',
        fillOpacity: 0.15,
        weight: 0.5
      }
    }
  }
}

// Render shapes and markers on map from status
let renderedActiveShape = null
let hasFittedBounds = false

const updateMapFromStatus = () => {
  if (!map) return

  // 1. Render active/defined area
  if (renderedActiveShape) {
    drawLayer.removeLayer(renderedActiveShape)
    renderedActiveShape = null
  }



  if (localCoordinates.value) {
    const isActive = status.value.active
    const strokeColor = isActive ? 'var(--ace-primary)' : 'var(--ace-secondary)'
    const fillColor = isActive ? 'var(--ace-primary)' : 'var(--ace-secondary)'
    
    if (localType.value === 'circle') {
      clearWedgeHandles()
      const center = localCoordinates.value.center
      const radius = localCoordinates.value.radius
      renderedActiveShape = L.circle(center, {
        radius: radius,
        color: strokeColor,
        weight: 3,
        fillColor: fillColor,
        fillOpacity: 0.15,
        interactive: false
      }).addTo(drawLayer)
      
      staticFitBoundOnce(renderedActiveShape.getBounds())
      
    } else if (localType.value === 'polygon') {
      clearWedgeHandles()
      const poly = localCoordinates.value.polygon
      renderedActiveShape = L.polygon(poly, {
        color: strokeColor,
        weight: 3,
        fillColor: fillColor,
        fillOpacity: 0.15,
        interactive: false
      }).addTo(drawLayer)
      
      staticFitBoundOnce(renderedActiveShape.getBounds())
    } else if (localType.value === 'wedge') {
      const coords = localCoordinates.value
      const pts = getWedgePolygonPoints(
        coords.origin[0], coords.origin[1],
        coords.heading, coords.fov_angle,
        coords.foreground_radius, coords.background_radius
      )
      renderedActiveShape = L.polygon(pts, {
        color: strokeColor,
        weight: 3,
        fillColor: fillColor,
        fillOpacity: 0.15,
        interactive: false
      }).addTo(drawLayer)
      
      updateWedgeHandles()
      
      staticFitBoundOnce(renderedActiveShape.getBounds())
    }
  } else {
    clearWedgeHandles()
  }

  // 2. Render recent strikes
  // Remove markers for strikes no longer in list
  const currentStrikeIds = new Set(status.value.recent_strikes.map(s => s.id))
  for (const id in strikeMarkers) {
    if (!currentStrikeIds.has(id)) {
      strikeGroup.removeLayer(strikeMarkers[id])
      delete strikeMarkers[id]
    }
  }

  // Add markers for new strikes and update existing ones
  status.value.recent_strikes.forEach(strike => {
    const style = getMarkerStyleForStrike(strike)
    if (!strikeMarkers[strike.id]) {
      const marker = L.circleMarker([strike.lat, strike.lon], style).addTo(strikeGroup)

      const dateStr = new Date(strike.dateTimeISO).toLocaleString()
      const distStr = strike.distance_meters !== null 
        ? `<br>Distance: <strong>${(strike.distance_meters/1000).toFixed(2)} km</strong>`
        : ''
      const intensityStr = strike.intensity !== null 
        ? `<br>Intensity: <strong>${strike.intensity} kA</strong>` 
        : ''
      
      marker.bindPopup(`
        <div style="font-family: var(--ace-font-body); padding: 5px;">
          <h4 style="margin: 0 0 5px; color: var(--ace-danger); font-family: var(--ace-font-heading);">⚡ Strike Detected</h4>
          Time: <strong>${dateStr}</strong>
          ${distStr}
          ${intensityStr}
          <br>Type: <strong>${strike.type}</strong>
          <br><br>
          <a href="https://www.google.com/maps/search/?api=1&query=${strike.lat},${strike.lon}" target="_blank" style="color: var(--ace-secondary); text-decoration: underline; font-weight: 700;">View on Google Maps</a>
        </div>
      `)

      strikeMarkers[strike.id] = marker
    } else {
      // Update styling of existing marker dynamically as it ages
      const marker = strikeMarkers[strike.id]
      marker.setStyle(style)
      marker.setRadius(style.radius)
    }
  })
  
  if (strikeGroup && typeof strikeGroup.bringToFront === 'function') {
    strikeGroup.bringToFront()
  }
  if (localType.value === 'wedge') {
    syncWedgeInputRefs()
  }
}

const staticFitBoundOnce = (bounds) => {
  if (!hasFittedBounds && bounds) {
    map.fitBounds(bounds, { padding: [50, 50] })
    hasFittedBounds = true
  }
}

watch(() => status.value.active, (newVal) => {
  if (!newVal) {
    hasFittedBounds = false
  }
})

const panToStrike = (strike) => {
  if (map && strike) {
    map.setView([strike.lat, strike.lon], 11)
    const marker = strikeMarkers[strike.id]
    if (marker) {
      marker.openPopup()
    }
  }
}

const formatTime = (isoString) => {
  return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// API Calls
const fetchConfig = async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/config`)
    if (resp.ok) {
      configStatus.value = await resp.json()
    }
  } catch (err) {
    console.error('Failed to fetch config status:', err)
  }
}

const saveConfig = async () => {
  savingConfig.value = true
  try {
    const resp = await fetch(`${API_BASE}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        xweather_api_key: configForm.value.xweather_api_key,
        discord_webhook: configForm.value.discord_webhook
      })
    })
    
    if (resp.ok) {
      alert('Configuration saved successfully!')
      configForm.value.xweather_api_key = ''
      configForm.value.discord_webhook = ''
      await fetchConfig()
      await fetchStatus()
    } else {
      const errData = await resp.json()
      alert(`Failed to save config: ${errData.detail || 'Unknown error'}`)
    }
  } catch (err) {
    alert(`Error saving configuration: ${err.message}`)
  } finally {
    savingConfig.value = false
  }
}

const testDiscordWebhook = async () => {
  testingWebhook.value = true
  try {
    const resp = await fetch(`${API_BASE}/api/test-discord`, { method: 'POST' })
    const data = await resp.json()
    if (resp.ok) {
      alert('Test alert sent successfully to Discord!')
    } else {
      alert(`Webhook Test Failed: ${data.detail || 'Unknown error'}`)
    }
  } catch (err) {
    alert(`Error: ${err.message}`)
  } finally {
    testingWebhook.value = false
  }
}

const startMonitoring = async () => {
  if (!localCoordinates.value) return
  
  try {
    const payload = {
      type: localType.value,
      coordinates: localCoordinates.value,
      timeout_hours: timeoutHours.value,
      interval_minutes: checkIntervalMinutes.value
    }
    
    const resp = await fetch(`${API_BASE}/api/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    
    if (resp.ok) {
      await fetchStatus()
    } else {
      const errData = await resp.json()
      alert(`Failed to start: ${errData.detail || 'Unknown error'}`)
    }
  } catch (err) {
    alert(`Network error starting monitoring: ${err.message}`)
  }
}

const stopMonitoring = async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/stop`, { method: 'POST' })
    if (resp.ok) {
      await fetchStatus()
    }
  } catch (err) {
    alert(`Error stopping: ${err.message}`)
  }
}
</script>

<template>
  <div class="sentinel-app">
    <!-- Sidebar / Control Panel -->
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-badge">⚡ FLASH</span>
        <h2>Flash Finder</h2>
        <p class="brand-sub">Real-time lightning threat monitoring</p>
      </div>

      <div class="control-groups">
        <!-- Configuration Card -->
        <div class="card control-card">
          <h3 class="card-title">1. Define Detection Area</h3>
          
          <div class="draw-actions">
            <button 
              class="ace-button ace-button--sm"
              :class="drawMode === 'circle' ? 'ace-button--secondary' : 'ace-button--ghost'"
              :disabled="status.active"
              @click="startDrawingCircle"
            >
              ⭕ Circle
            </button>
            <button 
              class="ace-button ace-button--sm"
              :class="drawMode === 'polygon' ? 'ace-button--secondary' : 'ace-button--ghost'"
              :disabled="status.active"
              @click="startDrawingPolygon"
            >
              ⬡ Polygon
            </button>
            <button 
              class="ace-button ace-button--sm"
              :class="drawMode === 'wedge' ? 'ace-button--secondary' : 'ace-button--ghost'"
              :disabled="status.active"
              @click="startDrawingWedge"
            >
              📐 Wedge
            </button>
            <button 
              class="ace-button ace-button--sm ace-button--danger"
              v-if="hasDrawnShape"
              :disabled="status.active"
              @click="clearDrawnShape"
            >
              🗑️ Clear
            </button>
          </div>
          
          <div v-if="drawMode === 'polygon' && tempPoints.length >= 3" class="complete-btn-container">
            <button 
              class="ace-button ace-button--sm ace-button--primary ace-button--block"
              @click="completePolygon"
            >
              ✓ Complete Polygon
            </button>
          </div>

          <div v-if="drawMode === 'wedge'" class="complete-btn-container">
            <button 
              class="ace-button ace-button--sm ace-button--primary ace-button--block"
              @click="placeWedgeAtCurrentLocation"
            >
              📍 Place at Current Location
            </button>
          </div>

          <div v-if="localType === 'wedge' || drawMode === 'wedge'" class="wedge-settings" style="margin-top: 0.8rem; border-top: 1px dashed var(--ace-border-strong); padding-top: 0.8rem; display: flex; flex-direction: column; gap: 0.8rem;">
            <!-- Focal Length -->
            <div class="ace-field">
              <label class="ace-field__label">Lens Focal Length (Full Frame Eq.)</label>
              <div class="focal-length-container">
                <select v-model.number="focalLength" :disabled="status.active" class="ace-select">
                  <option v-for="fl in commonFocalLengths" :key="fl.val" :value="fl.val">
                    {{ fl.label }}
                  </option>
                </select>
                <span class="fov-angle-display">{{ calculatedFovAngleText }}° FOV</span>
              </div>
              <div class="ace-field__hint">The focal length determines the horizontal field of view angle.</div>
            </div>

            <!-- Coordinate inputs -->
            <div class="wedge-coordinates-panel">
              <!-- Origin / Camera Location -->
              <div class="coordinate-group">
                <div class="coordinate-header">
                  <label class="ace-field__label">📷 Camera Location (Origin)</label>
                  <button 
                    class="ace-button ace-button--ghost ace-button--sm"
                    :class="drawMode === 'wedge_origin' ? 'ace-button--secondary' : ''"
                    :disabled="status.active"
                    @click="pickWedgeOriginOnMap"
                    type="button"
                    style="padding: 0.15rem 0.4rem; font-size: 0.72rem; min-height: auto;"
                  >
                    📍 Map Pick
                  </button>
                </div>
                <div class="coordinate-inputs">
                  <div class="coordinate-input-wrapper">
                    <span class="coord-label">Lat</span>
                    <input 
                      type="number" 
                      step="0.000001" 
                      v-model.number="wedgeOriginLat" 
                      @change="updateWedgeFromInputs"
                      :disabled="status.active"
                      class="ace-input ace-input--sm"
                    />
                  </div>
                  <div class="coordinate-input-wrapper">
                    <span class="coord-label">Lng</span>
                    <input 
                      type="number" 
                      step="0.000001" 
                      v-model.number="wedgeOriginLng" 
                      @change="updateWedgeFromInputs"
                      :disabled="status.active"
                      class="ace-input ace-input--sm"
                    />
                  </div>
                </div>
              </div>

              <!-- Focal Point / Target Location -->
              <div class="coordinate-group" style="margin-top: 0.6rem;">
                <div class="coordinate-header">
                  <label class="ace-field__label">🎯 Subject Focal Point</label>
                  <button 
                    class="ace-button ace-button--ghost ace-button--sm"
                    :class="drawMode === 'wedge_focal' ? 'ace-button--secondary' : ''"
                    :disabled="status.active"
                    @click="pickWedgeFocalOnMap"
                    type="button"
                    style="padding: 0.15rem 0.4rem; font-size: 0.72rem; min-height: auto;"
                  >
                    📍 Map Pick
                  </button>
                </div>
                <div class="coordinate-inputs">
                  <div class="coordinate-input-wrapper">
                    <span class="coord-label">Lat</span>
                    <input 
                      type="number" 
                      step="0.000001" 
                      v-model.number="wedgeFocalLat" 
                      @change="updateWedgeFromInputs"
                      :disabled="status.active"
                      class="ace-input ace-input--sm"
                    />
                  </div>
                  <div class="coordinate-input-wrapper">
                    <span class="coord-label">Lng</span>
                    <input 
                      type="number" 
                      step="0.000001" 
                      v-model.number="wedgeFocalLng" 
                      @change="updateWedgeFromInputs"
                      :disabled="status.active"
                      class="ace-input ace-input--sm"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
          
          <div v-if="drawHelpText" class="help-text">
            {{ drawHelpText }}
          </div>

          <div v-if="hasDrawnShape" class="shape-info">
            <span class="ace-badge ace-badge--info">Area Defined</span>
            <span class="shape-details">{{ shapeDetailsText }}</span>
          </div>
        </div>

        <!-- Monitoring Settings -->
        <div class="card control-card">
          <h3 class="card-title">2. Alert Configuration</h3>
          
          <div class="ace-field">
            <label class="ace-field__label">Timeout Duration</label>
            <div class="slider-container">
              <input 
                type="range" 
                min="1" 
                max="4" 
                step="0.5" 
                v-model.number="timeoutHours"
                :disabled="status.active"
                class="ace-input-slider"
              />
              <span class="slider-value">{{ timeoutHours }} hrs</span>
            </div>
            <div class="ace-field__hint">Monitoring will automatically stop after this duration.</div>
          </div>

          <div class="ace-field" style="margin-top: 1rem;">
            <label class="ace-field__label">Check Frequency</label>
            <div class="slider-container">
              <input 
                type="range" 
                min="1" 
                max="5" 
                step="1" 
                v-model.number="checkIntervalMinutes"
                :disabled="status.active"
                class="ace-input-slider"
              />
              <span class="slider-value">{{ checkIntervalMinutes }} min</span>
            </div>
            <div class="ace-field__hint">How often Flash Finder queries the API for new strikes.</div>
          </div>
          
          <div class="api-estimate">
            <span class="estimate-label">⚡ Estimated API Calls:</span>
            <span class="estimate-value">
              <strong>{{ estimatedApiCalls }}</strong> calls 
              <span class="estimate-percent">({{ freeTierPercentage }}% of free limit)</span>
            </span>
          </div>
        </div>

        <!-- Credentials Config Card -->
        <div class="card control-card">
          <h3 class="card-title">⚙️ Credentials Settings</h3>
          
          <div class="ace-field">
            <label class="ace-field__label">XWeather API Key</label>
            <input 
              type="password" 
              v-model="configForm.xweather_api_key"
              :placeholder="configStatus.xweather_configured ? configStatus.xweather_api_key_masked : 'Enter client_id_client_secret'"
              class="ace-input"
            />
          </div>

          <div class="ace-field">
            <label class="ace-field__label">Discord Webhook</label>
            <input 
              type="password" 
              v-model="configForm.discord_webhook"
              :placeholder="configStatus.discord_configured ? configStatus.discord_webhook_masked : 'Enter Discord Webhook URL'"
              class="ace-input"
            />
          </div>

          <div class="config-actions">
            <button 
              class="ace-button ace-button--sm ace-button--primary ace-button--block"
              @click="saveConfig"
              :disabled="savingConfig"
            >
              {{ savingConfig ? 'Saving...' : 'Save Configuration' }}
            </button>
          </div>
          
          <div class="test-webhook-container" style="margin-top: 0.5rem;" v-if="configStatus.discord_configured">
            <button 
              class="ace-button ace-button--sm ace-button--ghost ace-button--block"
              @click="testDiscordWebhook"
              :disabled="testingWebhook"
            >
              {{ testingWebhook ? 'Sending Test...' : 'Test Webhook' }}
            </button>
          </div>
        </div>

        <!-- Action / Status Card -->
        <div class="card status-card">
          <div class="status-header">
            <h3 class="card-title">Flash Finder Status</h3>
            <span 
              class="ace-badge" 
              :class="status.active ? 'ace-badge--success' : 'ace-badge--neutral'"
            >
              {{ status.active ? 'Active' : 'Inactive' }}
            </span>
          </div>

          <div class="status-stats" v-if="status.active">
            <div class="stat-item">
              <span class="stat-label">Checks</span>
              <span class="stat-value">{{ status.checks_count }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">Strikes</span>
              <span class="stat-value danger-text">{{ status.detections_count }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">Remaining</span>
              <span class="stat-value">{{ timeRemainingText }}</span>
            </div>
          </div>

          <div class="start-stop-actions">
            <div v-if="!isConfigured" class="config-warning">
              ⚠️ Please configure XWeather and Discord keys below to start monitoring.
            </div>
            <button 
              v-if="!status.active"
              class="ace-button ace-button--md ace-button--primary ace-button--block"
              :disabled="!hasDrawnShape || !isConfigured"
              @click="startMonitoring"
            >
              ▶ Start Monitoring
            </button>
            <button 
              v-else
              class="ace-button ace-button--md ace-button--danger ace-button--block"
              @click="stopMonitoring"
            >
              ⏹ Stop Monitoring
            </button>
          </div>
        </div>
      </div>
    </aside>

    <!-- Main Content Area -->
    <main class="content">
      <!-- Leaflet Map -->
      <div class="map-container">
        <div id="map"></div>
        
        <!-- Map Legend -->
        <div class="map-legend">
          <div class="legend-title">⚡ Strike Age Legend</div>
          <div class="legend-items">
            <div class="legend-item-style">
              <span class="legend-dot dot-danger"></span>
              <span class="legend-text">Active Front (&lt; 1m)</span>
            </div>
            <div class="legend-item-style">
              <span class="legend-dot dot-warning"></span>
              <span class="legend-text">Recent (&lt; 15m)</span>
            </div>
            <div class="legend-item-style">
              <span class="legend-dot dot-transition"></span>
              <span class="legend-text">Intermediate (&lt; 30m)</span>
            </div>
            <div class="legend-item-style">
              <span class="legend-dot dot-muted"></span>
              <span class="legend-text">Historical (&gt; 30m)</span>
            </div>
            <div class="legend-item-style" style="border-top: 1px solid rgba(255, 255, 255, 0.05); padding-top: 0.3rem; margin-top: 0.1rem;">
              <span class="legend-dot dot-info"></span>
              <span class="legend-text">Outside target area</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Logs and Detections Tabs -->
      <div class="details-panel">
        <div class="details-tabs">
          <button 
            class="tab-btn" 
            :class="{ active: activeTab === 'strikes' }"
            @click="activeTab = 'strikes'"
          >
            ⚡ Strikes ({{ status.recent_strikes.length }})
          </button>
          <button 
            class="tab-btn" 
            :class="{ active: activeTab === 'logs' }"
            @click="activeTab = 'logs'"
          >
            📋 Logs ({{ status.logs.length }})
          </button>
          <button 
            class="tab-btn" 
            :class="{ active: activeTab === 'docs' }"
            @click="activeTab = 'docs'"
          >
            📖 Docs
          </button>
        </div>

        <div class="tab-content card">
          <!-- Strikes List -->
          <div v-if="activeTab === 'strikes'" class="strikes-tab">
            <div v-if="status.recent_strikes.length === 0" class="empty-state">
              No lightning strikes detected in this session.
            </div>
            <div v-else class="list-wrapper">
              <div 
                v-for="strike in sortedStrikes" 
                :key="strike.id" 
                class="strike-item"
                @click="panToStrike(strike)"
              >
                <div class="strike-left">
                  <div class="strike-meta">
                    <span class="strike-time">{{ formatTime(strike.dateTimeISO) }}</span>
                    <span class="strike-type ace-badge ace-badge--neutral">Type: {{ strike.type }}</span>
                    <span class="strike-intensity ace-badge ace-badge--danger" v-if="strike.intensity">
                      {{ strike.intensity }} kA
                    </span>
                  </div>
                  <div class="strike-loc">
                    <span>📍 Lat: {{ strike.lat.toFixed(4) }}, Lon: {{ strike.lon.toFixed(4) }}</span>
                    <span v-if="strike.distance_meters !== null" class="strike-dist">
                      ({{ (strike.distance_meters / 1000).toFixed(2) }} km away)
                    </span>
                  </div>
                </div>
                <a 
                  :href="`https://www.google.com/maps/search/?api=1&query=${strike.lat},${strike.lon}`" 
                  target="_blank" 
                  class="map-link ace-button ace-button--quiet ace-button--link"
                  @click.stop
                >
                  Open Maps ↗
                </a>
              </div>
            </div>
          </div>

          <!-- Logs List -->
          <div v-if="activeTab === 'logs'" class="logs-tab">
            <div v-if="status.logs.length === 0" class="empty-state">
              No logs available.
            </div>
            <div v-else class="list-wrapper logs-list">
              <div v-for="(log, idx) in status.logs.slice().reverse()" :key="idx" class="log-item">
                {{ log }}
              </div>
            </div>
          </div>

          <!-- Docs Tab -->
          <div v-if="activeTab === 'docs'" class="docs-tab">
            <div class="docs-container">
              <section class="docs-section">
                <h4>🔑 How to Acquire an XWeather API Key</h4>
                <ol>
                  <li>Go to the <a href="https://www.xweather.com/" target="_blank" class="docs-link">XWeather Website ↗</a> and sign up for an account.</li>
                  <li>Log in to your contributor dashboard and navigate to <strong>API Credentials</strong> or <strong>Subscriptions</strong>.</li>
                  <li>Acquire your <strong>Client ID</strong> and <strong>Client Secret</strong>.</li>
                  <li>Combine them in the format <code>CLIENT_ID_CLIENT_SECRET</code> (separated by an underscore, e.g. <code>18VzzDS0Mi2ikqdJTn7bt_P4CcQPTSHPkwIRKZ4lXe3ZMDda3suJXY9QOiKieD</code>).</li>
                  <li>Paste the combined key into the <strong>XWeather API Key</strong> field in the <strong>Credentials Settings</strong> card below.</li>
                </ol>
              </section>

              <section class="docs-section">
                <h4>🔔 How to Set Up a Discord Webhook</h4>
                <ol>
                  <li>Open Discord and navigate to the server and channel where you want to receive alerts.</li>
                  <li>Click the gear icon (⚙️) next to the channel name to open <strong>Channel Settings</strong>.</li>
                  <li>Go to <strong>Integrations</strong> and click <strong>Webhooks</strong>.</li>
                  <li>Click <strong>New Webhook</strong>, choose a name and avatar, and ensure the correct channel is selected.</li>
                  <li>Click <strong>Copy Webhook URL</strong>.</li>
                  <li>Paste the URL into the <strong>Discord Webhook</strong> field in the <strong>Credentials Settings</strong> card below.</li>
                </ol>
              </section>

              <section class="docs-section">
                <h4>🎯 How to Use the Interface</h4>
                <ul>
                  <li><strong>Define Detection Area:</strong> Select ⭕ <strong>Circle</strong>, ⬡ <strong>Polygon</strong>, or 📐 <strong>Wedge</strong>.
                    <ul>
                      <li><em>Circle:</em> Click on the map to place the center, move the mouse to choose the radius, and click again to lock it.</li>
                      <li><em>Polygon:</em> Click on the map to place vertices (at least 3). Click the first point (lime dot) or click <strong>✓ Complete Polygon</strong> in the sidebar to close the shape.</li>
                      <li><em>Wedge (Camera FOV):</em> Click on the map or click <strong>📍 Place at Current Location</strong> to initialize the camera position. Once placed, select your full-frame equivalent focal length from the sidebar to set the horizontal angle of view. Drag the camera marker 📷 to move the origin, drag the inner circle to adjust the foreground radius, and drag the outer circle to sweep the heading and background radius.</li>
                    </ul>
                  </li>
                  <li><strong>Configure Duration:</strong> Use the slider to set a timeout from 1 to 4 hours. The interface shows the estimated API call count and monthly free limit percentage.</li>
                  <li><strong>Start Monitoring:</strong> Click <strong>▶ Start Monitoring</strong>. Flash Finder will query XWeather every 5 minutes. If strikes fall inside the region, a rich Discord alert with map links is sent.</li>
                  <li><strong>Visual Age-Fading & Legend:</strong> Strikes are color-coded based on their age in minutes to track storm fronts:
                    <ul>
                        <li>🔴 <strong>Active Front (&lt; 1 min)</strong>: Vibrant neon-red circles.</li>
                        <li>🟠 <strong>Recent (&lt; 15 mins)</strong>: Warm orange circles.</li>
                        <li>🟡 <strong>Intermediate (&lt; 30 mins)</strong>: Faded/dark orange circles.</li>
                        <li>⚪ <strong>Historical (&gt; 30 mins)</strong>: Faded gray circles.</li>
                        <li>🔵 <strong>Encroaching Buffer (Outside target area)</strong>: Neon-cyan circles showing storm proximity.</li>
                    </ul>
                  </li>
                </ul>
              </section>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.sentinel-app {
  display: grid;
  grid-template-columns: 380px 1fr;
  height: 100vh;
  background: var(--ace-bg);
  color: var(--ace-text);
  overflow: hidden;
}

.sidebar {
  background: var(--ace-bg-panel);
  border-right: 1px solid var(--ace-border-strong);
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  overflow-y: auto;
}

.brand {
  border-bottom: 1px solid var(--ace-border);
  padding-bottom: 1rem;
}

.brand-badge {
  background: var(--ace-primary-soft);
  color: var(--ace-primary);
  font-weight: 800;
  font-size: 0.7rem;
  padding: 0.2rem 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid var(--ace-primary);
  display: inline-block;
  margin-bottom: 0.5rem;
}

.brand h2 {
  font-family: var(--ace-font-heading);
  margin: 0;
  font-size: 1.5rem;
}

.brand-sub {
  color: var(--ace-text-subtle);
  font-size: 0.8rem;
  margin: 0.25rem 0 0;
}

.control-groups {
  display: flex;
  flex-direction: column;
  gap: 1.2rem;
}

.control-card {
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}

.card-title {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--ace-text);
}

.draw-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.draw-actions button {
  flex: 1 0 auto;
}

.complete-btn-container {
  margin-top: 0.2rem;
}

.help-text {
  font-size: 0.76rem;
  color: var(--ace-secondary);
  background: var(--ace-secondary-soft);
  padding: 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid var(--ace-focus);
}

.shape-info {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.78rem;
  background: rgba(255, 255, 255, 0.03);
  padding: 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid var(--ace-border);
}

.shape-details {
  color: var(--ace-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex-grow: 1;
}

.slider-container {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  margin-top: 0.3rem;
}

.ace-input-slider {
  flex-grow: 1;
  accent-color: var(--ace-primary);
}

.slider-value {
  font-weight: 700;
  color: var(--ace-primary);
  font-size: 0.9rem;
  min-width: 3.5rem;
  text-align: right;
}

.status-card {
  padding: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.status-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.status-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.5rem;
  background: rgba(0, 0, 0, 0.2);
  padding: 0.8rem;
  border-radius: var(--ace-radius-md);
  border: 1px solid var(--ace-border);
}

.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.stat-label {
  font-size: 0.68rem;
  color: var(--ace-text-subtle);
  text-transform: uppercase;
  font-weight: 700;
}

.stat-value {
  font-size: 1rem;
  font-weight: 800;
  color: var(--ace-text);
}

.danger-text {
  color: var(--ace-danger);
}

.content {
  display: grid;
  grid-template-rows: 1fr 280px;
  height: 100vh;
  overflow: hidden;
}

.map-container {
  position: relative;
  width: 100%;
  height: 100%;
}

#map {
  width: 100%;
  height: 100%;
  border: none;
  border-radius: 0;
}

.details-panel {
  background: var(--ace-bg-elevated);
  border-top: 1px solid var(--ace-border-strong);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.details-tabs {
  display: flex;
  background: var(--ace-bg-panel);
  border-bottom: 1px solid var(--ace-border);
}

.tab-btn {
  background: none;
  border: none;
  color: var(--ace-text-muted);
  padding: 0.8rem 1.5rem;
  font-weight: 700;
  font-size: 0.85rem;
  cursor: pointer;
  border-right: 1px solid var(--ace-border);
  transition: all 0.15s;
}

.tab-btn:hover {
  color: var(--ace-text);
  background: rgba(255, 255, 255, 0.02);
}

.tab-btn.active {
  color: var(--ace-primary);
  background: var(--ace-bg-elevated);
  border-bottom: 2px solid var(--ace-primary);
}

.tab-content {
  flex-grow: 1;
  overflow-y: auto;
  border-radius: 0;
  border: none;
  padding: 1rem;
}

.empty-state {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
  color: var(--ace-text-subtle);
  font-size: 0.88rem;
}

.list-wrapper {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.strike-item {
  background: var(--ace-bg-panel);
  border: 1px solid var(--ace-border);
  border-radius: var(--ace-radius-md);
  padding: 0.8rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
  transition: all 0.15s;
}

.strike-item:hover {
  border-color: var(--ace-secondary-soft);
  background: rgba(255, 255, 255, 0.02);
  transform: translateY(-1px);
}

.strike-left {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.strike-meta {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.strike-time {
  font-weight: 700;
  font-size: 0.84rem;
}

.strike-loc {
  font-size: 0.78rem;
  color: var(--ace-text-muted);
  display: flex;
  gap: 0.8rem;
}

.strike-dist {
  color: var(--ace-secondary);
  font-weight: 600;
}

.map-link {
  font-size: 0.8rem;
  align-self: center;
}

.logs-list {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.78rem;
  color: var(--ace-text-muted);
  gap: 0.3rem;
}

.log-item {
  padding: 0.2rem 0.4rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.02);
}

.config-warning {
  font-size: 0.76rem;
  color: var(--ace-danger);
  background: var(--ace-danger-soft);
  padding: 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid rgba(246, 81, 29, 0.3);
  margin-bottom: 0.8rem;
  text-align: center;
}

.api-estimate {
  margin-top: 0.8rem;
  font-size: 0.76rem;
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--ace-border-strong);
  border-radius: var(--ace-radius-sm);
  padding: 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.estimate-label {
  color: var(--ace-text-subtle);
  font-weight: 700;
  text-transform: uppercase;
  font-size: 0.68rem;
}

.estimate-value {
  color: var(--ace-primary);
  font-weight: 700;
}

.estimate-percent {
  color: var(--ace-text-muted);
  font-weight: 500;
}

.docs-container {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  padding: 0.5rem;
  max-width: 800px;
}

.docs-section {
  border-bottom: 1px solid var(--ace-border);
  padding-bottom: 1.2rem;
}

.docs-section:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.docs-section h4 {
  font-family: var(--ace-font-heading);
  margin: 0 0 0.6rem;
  color: var(--ace-secondary);
  font-size: 1.05rem;
}

.docs-section ol, .docs-section ul {
  margin: 0;
  padding-left: 1.3rem;
  font-size: 0.84rem;
  color: var(--ace-text-muted);
  line-height: 1.5;
}

.docs-section li {
  margin-bottom: 0.5rem;
}

.docs-section code {
  background: var(--ace-bg-panel);
  color: var(--ace-secondary);
  border: 1px solid var(--ace-border);
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  font-size: 0.78rem;
}

.docs-link {
  color: var(--ace-secondary);
  text-decoration: underline;
  font-weight: 700;
}

.docs-link:hover {
  color: var(--ace-text);
}

.map-legend {
  position: absolute;
  bottom: 20px;
  right: 20px;
  z-index: 1000;
  background: var(--ace-bg-panel);
  border: 1px solid var(--ace-border-strong);
  border-radius: var(--ace-radius-sm);
  padding: 0.8rem;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
  font-family: var(--ace-font-body);
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  pointer-events: none;
}

.legend-title {
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--ace-text-subtle);
  border-bottom: 1px solid var(--ace-border);
  padding-bottom: 0.3rem;
  margin-bottom: 0.2rem;
}

.legend-items {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.legend-item-style {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot-danger {
  background: var(--ace-danger);
  box-shadow: 0 0 8px var(--ace-danger);
}

.dot-warning {
  background: #f69d1d;
  box-shadow: 0 0 6px #f69d1d;
}

.dot-transition {
  background: #b06b0d;
  box-shadow: 0 0 5px #b06b0d;
}

.dot-info {
  background: var(--ace-secondary);
  box-shadow: 0 0 6px var(--ace-secondary);
}

.dot-muted {
  background: var(--ace-text-subtle);
  opacity: 0.6;
}

.legend-text {
  font-size: 0.74rem;
  color: var(--ace-text);
  white-space: nowrap;
}

/* Wedge handle custom styles */
.focal-length-container {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  margin-top: 0.3rem;
}

.fov-angle-display {
  font-weight: 700;
  color: var(--ace-primary);
  font-size: 0.9rem;
  white-space: nowrap;
}

.ace-select {
  background: var(--ace-bg-panel);
  color: var(--ace-text);
  border: 1px solid var(--ace-border-strong);
  border-radius: var(--ace-radius-sm);
  padding: 0.5rem;
  font-family: var(--ace-font-body);
  font-size: 0.85rem;
  width: 100%;
  outline: none;
  cursor: pointer;
  transition: border-color 0.15s;
}

.ace-select:focus {
  border-color: var(--ace-primary);
}

.ace-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* Custom styles for leaflet divIcon elements (avoid default white background / borders) */
.custom-wedge-handle {
  background: transparent !important;
  border: none !important;
}

.wedge-handle-origin {
  background: var(--ace-secondary);
  color: white;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  box-shadow: 0 0 6px rgba(0,0,0,0.6);
  cursor: grab;
}

.wedge-handle-origin:active {
  cursor: grabbing;
}

.wedge-handle-fg {
  background: #f69d1d;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid white;
  box-shadow: 0 0 5px rgba(0,0,0,0.5);
  cursor: grab;
}

.wedge-handle-bg {
  background: var(--ace-primary);
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid white;
  box-shadow: 0 0 5px rgba(0,0,0,0.5);
  cursor: grab;
}

.wedge-coordinates-panel {
  display: flex;
  flex-direction: column;
  background: rgba(255, 255, 255, 0.015);
  border: 1px solid var(--ace-border-strong);
  border-radius: var(--ace-radius-md);
  padding: 0.8rem;
  gap: 0.5rem;
}

.coordinate-group {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.coordinate-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.2rem;
}

.coordinate-header .ace-field__label {
  margin: 0;
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--ace-text-subtle);
}

.coordinate-inputs {
  display: flex;
  gap: 0.5rem;
}

.coordinate-input-wrapper {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  background: var(--ace-bg-panel);
  border: 1px solid var(--ace-border-strong);
  border-radius: var(--ace-radius-sm);
  padding-left: 0.45rem;
}

.coord-label {
  font-size: 0.72rem;
  color: var(--ace-text-muted);
  font-weight: 700;
  text-transform: uppercase;
}

.coordinate-input-wrapper .ace-input--sm {
  border: none !important;
  background: transparent !important;
  padding: 0.35rem 0.3rem;
  font-size: 0.8rem;
  width: 100%;
}

.wedge-handle-focal {
  background: var(--ace-danger);
  color: white;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 2px solid white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  box-shadow: 0 0 6px rgba(0,0,0,0.6);
  cursor: grab;
}

.wedge-handle-focal:active {
  cursor: grabbing;
}
</style>
