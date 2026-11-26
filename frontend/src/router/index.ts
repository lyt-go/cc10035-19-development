import { createRouter, createWebHistory } from 'vue-router'

import IncidentDispatchView from '@/views/IncidentDispatchView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const incidentDispatchRoutes = {
  path: '/incident-dispatch',
  name: 'incident-dispatch',
  component: IncidentDispatchView,
  alias: [
    '/incident_dispatch',
    '/recovery-dispatch',
    '/recovery_dispatch',
    '/gap-dispatch',
    '/gap_dispatch',
    '/gateway-incident',
    '/gateway_incident',
    '/gateway-incidents',
  ],
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    incidentDispatchRoutes,
    { path: '/gateway_mgmt/incident-dispatch', name: 'gateway-incident-dispatch', component: IncidentDispatchView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
