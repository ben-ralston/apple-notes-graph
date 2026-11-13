import { mount } from 'svelte';
import './tokens.css';
import './chrome.css';
import App from './App.svelte';

mount(App, { target: document.getElementById('app') });
