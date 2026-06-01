import './styles/tokens.css';
import './styles/base.css';
import { mount } from 'svelte';
import App from './App.svelte';

const target = document.getElementById('app');
if (!target) {
  throw new Error('DriftScribe UI: #app mount point not found');
}

const app = mount(App, { target });

export default app;
