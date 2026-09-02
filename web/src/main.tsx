import { createRoot } from 'react-dom/client';

import { App } from './app';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('no #root in the page');
createRoot(root).render(<App />);
