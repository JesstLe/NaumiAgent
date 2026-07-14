import { BrowserRouter } from 'react-router-dom'
import { AppRoutes } from '@/routes'
import '@/i18n'
import '@/index.css'

function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  )
}

export default App
