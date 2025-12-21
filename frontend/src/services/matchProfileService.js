/**
 * Match Profile Service
 * 
 * API client for match profile operations
 */

import axios from 'axios';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

/**
 * Get all match profiles
 */
export const getAllProfiles = async () => {
  const response = await axios.get(`${API_BASE_URL}/api/match-profiles`);
  return response.data;
};

/**
 * Get a specific match profile by ID
 */
export const getProfile = async (id) => {
  const response = await axios.get(`${API_BASE_URL}/api/match-profiles/${id}`);
  return response.data;
};

/**
 * Create a new match profile
 */
export const createProfile = async (profileData) => {
  const response = await axios.post(`${API_BASE_URL}/api/match-profiles`, profileData);
  return response.data;
};

/**
 * Update an existing match profile
 */
export const updateProfile = async (id, profileData) => {
  const response = await axios.put(`${API_BASE_URL}/api/match-profiles/${id}`, profileData);
  return response.data;
};

/**
 * Delete a match profile
 */
export const deleteProfile = async (id) => {
  await axios.delete(`${API_BASE_URL}/api/match-profiles/${id}`);
};

/**
 * Test a match profile without applying changes
 */
export const testProfile = async (id) => {
  const response = await axios.post(`${API_BASE_URL}/api/match-profiles/${id}/test`);
  return response.data;
};

/**
 * Execute a match profile manually
 */
export const executeProfile = async (id) => {
  const response = await axios.post(`${API_BASE_URL}/api/match-profiles/${id}/execute`);
  return response.data;
};

/**
 * Get available node types and their schemas
 */
export const getNodeTypes = async () => {
  const response = await axios.get(`${API_BASE_URL}/api/match-profiles/node-types`);
  return response.data;
};

/**
 * Validate a match profile configuration
 */
export const validateProfile = async (profileData) => {
  const response = await axios.post(`${API_BASE_URL}/api/match-profiles/validate`, profileData);
  return response.data;
};

export default {
  getAllProfiles,
  getProfile,
  createProfile,
  updateProfile,
  deleteProfile,
  testProfile,
  executeProfile,
  getNodeTypes,
  validateProfile,
};
