# Channel Configuration Features

## Overview
The Channel Configuration page provides comprehensive tools for managing channels, their regex patterns, ordering, and group-level settings through three main tabs.

## Tabs

### 1. Regex Configuration Tab
Manage stream matching patterns for individual channels.

### 2. Group Management Tab
Control stream matching and checking settings for entire channel groups.

### 3. Channel Order Tab
Organize and reorder channels using drag-and-drop functionality.

## Tab Features

### Regex Configuration Tab Features

#### 1. Table-Based Layout
The Regex Configuration tab now features a clean, table-based interface for managing patterns:

**Column Headers**:
- **Select**: Checkbox for multi-select functionality
- **#**: Channel number
- **Logo**: Channel logo/icon (shows first letter if logo unavailable)
- **Channel Name**: Name of the channel
- **Channel Group**: Group the channel belongs to
- **Regex Patterns**: Count of configured patterns
- **Actions**: Dropdown menu for channel actions

#### 2. Multi-Select Functionality
- **Individual Selection**: Click checkbox on any row to select/deselect a channel
- **Select All**: Check the header checkbox to select all visible channels on current page
- **Deselect All**: Uncheck the header checkbox or use "Deselect All" button
- **Selection Counter**: Badge showing number of selected channels

#### 3. Group Filtering and Sorting
- **Filter by Group**: Dropdown to show only channels from a specific group
  - "All Groups" option to show channels from all groups
  - Individual group options for focused management
- **Sort by Group**: Checkbox to organize channels by group name
  - Maintains channel number order within each group
  - Makes it easy to work with channels from the same group

#### 4. Mass Regex Assignment
Add a single regex pattern to multiple channels at once:

1. **Select Channels**: Use checkboxes to select target channels
2. **Click "Add Regex to Selected"**: Opens bulk assignment dialog
3. **Enter Pattern**: Type your regex pattern
4. **Use Variables**: Include `{CHANNEL_NAME}` to create reusable patterns (legacy `CHANNEL_NAME` format also supported)
5. **Apply**: Pattern is added to all selected channels

**Pattern Variable Support**:
- `{CHANNEL_NAME}` (recommended) or `CHANNEL_NAME` (legacy) is replaced with each channel's actual name at match time
- Example: Pattern `.*{CHANNEL_NAME}.*` becomes:
  - `.*ESPN.*` for channel "ESPN"
  - `.*CNN.*` for channel "CNN"
  - `.*ABC.*` for channel "ABC"
- One pattern works for multiple channels with different names
- Reduces duplication and makes pattern management easier
- **Live Preview**: The live regex preview automatically substitutes `{CHANNEL_NAME}` or `CHANNEL_NAME` with the actual channel name, so you can see what streams will be matched in real-time
- **Special Characters**: Channel names with special regex characters (like `+`, `.`, `*`, etc.) are automatically escaped to prevent regex errors

#### 5. Search/Filter Field
- **Location**: Top of the page
- **Search across**:
  - Channel Number (e.g., "101", "5")
  - Channel Name (e.g., "ESPN", "CNN")
  - Channel Group (e.g., "Sports", "News")
- **Real-time filtering**: Table updates as you type
- **Case insensitive**: Works regardless of case

#### 6. Pagination
- **Items per page**: Choose 10, 20, 50, or 100 channels per page
- **Page navigation**: First, Previous, Page numbers, Next, Last
- **Current page indicator**: Shows which page you're on
- **Results counter**: Displays "Showing X-Y of Z channels"

#### 7. Pattern Management Per Channel
Click the Actions dropdown menu (⋮) on any channel row to:
- **View/Edit Regex Rules**: Opens dialog to view, add, edit, or delete regex patterns
- **Toggle Stream Matching**: Enable or disable stream matching for this channel
- **Toggle Stream Checking**: Enable or disable stream checking for this channel

The dropdown menu provides:
- Quick access to all channel-specific actions
- Visual indicators (switches) showing current matching and checking states
- Immediate feedback via toast notifications when settings are changed

#### 8. Channel Logo Display
Each row displays the channel's logo or icon:
- **Logo Source**: Fetched from Dispatcharr and cached locally
- **Fallback Display**: If no logo is available, shows the first letter of the channel name
- **Performance**: Logos are cached in browser localStorage for fast loading
- **Consistent Design**: Matches the logo display style from Channel Order tab

### Example Workflows

#### Workflow 1: Add Pattern to Multiple News Channels
1. Set "Filter Group" to "News"
2. Click "Select All" to select all news channels
3. Click "Add Regex to Selected"
4. Enter pattern: `.*{CHANNEL_NAME}.*|.*NEWS.*`
5. Click "Add to X Channels"
6. Pattern is now applied to all selected news channels

#### Workflow 2: Find and Configure Channels Without Patterns
1. Sort channels by regex pattern count (table shows pattern count in badge)
2. Select channels showing "No patterns"
3. Use bulk assignment to add a general pattern
4. Refine individual patterns as needed

#### Workflow 3: Organize by Group and Configure
1. Enable "Sort by Group" checkbox
2. Channels are now grouped and sorted
3. Set "Filter Group" to focus on one group
4. Configure patterns for that group
5. Move to next group and repeat

## UI Components

### Table Layout
```
+--------+-----+------+--------------+---------------+------------------+---------+
| Select | #   | Logo | Channel Name | Channel Group | Regex Patterns   | Actions |
+--------+-----+------+--------------+---------------+------------------+---------+
| ☐      | 5   | [A]  | ABC News     | News          | 2 patterns       | ⋮       |
| ☑      | 101 | [E]  | ESPN         | Sports        | 1 pattern        | ⋮       |
| ☑      | 505 | [C]  | CNN          | News          | No patterns      | ⋮       |
+--------+-----+------+--------------+---------------+------------------+---------+
```

### Actions Dropdown Menu
When clicking the Actions menu (⋮), you see:
```
┌─ Channel Actions ─────────────────────────┐
│ 👁️ View/Edit Regex Rules                  │
├───────────────────────────────────────────┤
│ Settings                                  │
│ Stream Matching              [Toggle ON]  │
│ Stream Checking              [Toggle ON]  │
└───────────────────────────────────────────┘
```

### Filter and Selection Bar
```
┌────────────────────────────────────────────────────────────────────────────┐
│ Filter Group: [All Groups ▼]  ☐ Sort by Group                            │
│                                                                            │
│ 2 selected  [Select All]  [Deselect All]  [➕ Add Regex to Selected]    │
└────────────────────────────────────────────────────────────────────────────┘
```

### Bulk Pattern Dialog
```
┌─ Add Regex Pattern to Multiple Channels ───────────────────────────────────┐
│ This pattern will be added to 2 selected channels.                         │
│ Use {CHANNEL_NAME} to insert each channel's name into the pattern.      │
│                                                                             │
│ Regex Pattern                                                               │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ .*{CHANNEL_NAME}.*                                                    │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│ Example:                                                                    │
│ Pattern: .*{CHANNEL_NAME}.*                                              │
│ For channel "ESPN", matches: .*ESPN.*                                      │
│ For channel "CNN", matches: .*CNN.*                                        │
│                                                                             │
│                                      [Cancel]  [Add to 2 Channels]         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Search Field
```
┌────────────────────────────────────────────────────┐
│ 🔍 Search channels by name, number, or ID...      │
└────────────────────────────────────────────────────┘
```

## Technical Implementation

### State Management
```javascript
// Multi-select state
const [selectedChannels, setSelectedChannels] = useState(new Set());

// Filtering and sorting
const [searchQuery, setSearchQuery] = useState('');
const [filterByGroup, setFilterByGroup] = useState('all');
const [sortByGroup, setSortByGroup] = useState(false);

// Bulk assignment
const [bulkDialogOpen, setBulkDialogOpen] = useState(false);
const [bulkPattern, setBulkPattern] = useState('');
```

### Backend API

#### Bulk Assignment Endpoint
```
POST /api/regex-patterns/bulk
Content-Type: application/json

{
  "channel_ids": [1, 2, 3],
  "regex_patterns": [".*{CHANNEL_NAME}.*"]
}

Response:
{
  "message": "Successfully added patterns to 3 channel(s)",
  "success_count": 3,
  "total_channels": 3
}
```

#### Pattern Variable Substitution
The backend substitutes both `{CHANNEL_NAME}` and `CHANNEL_NAME` at match time:
```python
def _substitute_channel_variables(self, pattern: str, channel_name: str) -> str:
    """Substitute channel name variables in a regex pattern."""
    escaped_channel_name = re.escape(channel_name)
    # Support both formats
    result = pattern.replace('{CHANNEL_NAME}', escaped_channel_name)
    result = result.replace('CHANNEL_NAME', escaped_channel_name)
    return result
```

### Filtering Logic
```javascript
const displayChannels = orderedChannels.filter(channel => {
  // Apply group filter
  if (filterByGroup !== 'all' && channel.channel_group_id !== parseInt(filterByGroup)) {
    return false;
  }
  
  // Apply search filter
  if (searchQuery) {
    const query = searchQuery.toLowerCase();
    const matchesName = channel.name.toLowerCase().includes(query);
    const matchesNumber = String(channel.channel_number).includes(query);
    const matchesGroup = groupName.toLowerCase().includes(query);
    return matchesName || matchesNumber || matchesGroup;
  }
  
  return true;
});

// Sort by group if enabled
if (sortByGroup) {
  displayChannels.sort((a, b) => {
    const groupA = getGroupName(a.channel_group_id);
    const groupB = getGroupName(b.channel_group_id);
    return groupA.localeCompare(groupB) || (a.channel_number - b.channel_number);
  });
}
```

## User Workflow Examples

### Example 1: Find a Specific Channel
1. Type channel name or number in search field
2. Table filters in real-time
3. Edit or add patterns as needed

### Example 2: Sort by Pattern Count
1. Click "Patterns" column header
2. Channels with most patterns appear first (descending)
3. Click again to see channels with fewest patterns (ascending)
4. Identify channels needing pattern configuration

### Example 3: Review All Enabled Channels
1. Click "Status" column header
2. All enabled channels grouped together
3. Review and manage active patterns

## Benefits

1. **Efficient Mass Configuration**: Add patterns to hundreds of channels in seconds
2. **Reusable Patterns**: One pattern with `{CHANNEL_NAME}` works for many channels
3. **Better Organization**: Group filtering and sorting for focused management
4. **Faster Navigation**: Multi-select and bulk operations save time
5. **Clear Visual Feedback**: Table layout makes it easy to see pattern status
6. **Scalability**: Works efficiently with hundreds of channels
7. **Reduced Duplication**: Pattern variables eliminate redundant configurations
8. **Visual Channel Recognition**: Logo display helps quickly identify channels
9. **Quick Settings Access**: Dropdown menu provides one-click access to all channel actions
10. **Immediate Feedback**: Toggle switches show current state and update instantly

## Group Management Tab Features

### Overview
The Group Management tab allows you to control stream matching and checking behavior for entire channel groups at once, providing a more efficient way to manage large numbers of channels.

### Group Settings

Each channel group can be configured with two independent settings:

#### 1. Stream Matching
- **Enabled**: Channels in this group will be included in stream matching operations
- **Disabled**: Channels in this group will not participate in stream matching

#### 2. Stream Checking
- **Enabled**: Streams for channels in this group will be quality checked
- **Disabled**: Streams for channels in this group will skip quality checking

### Visibility Rules

**Important**: When BOTH settings (Stream Matching AND Stream Checking) are disabled for a group:
- All channels from that group will be **hidden** from the Regex Configuration tab
- All channels from that group will be **hidden** from the Channel Order tab
- This helps keep your interface clean by only showing channels that are actively being managed

### Group Card Display

Each group shows:
- **Group Name**: The name of the channel group
- **Channel Count**: Number of channels in the group
- **Group ID**: The unique identifier for the group
- **Settings Controls**: Dropdowns to enable/disable matching and checking
- **Warning Badge**: Displayed when both settings are disabled

### Use Cases

#### Example 1: Disable Sports Channels
1. Navigate to Group Management tab
2. Find the "Sports" group
3. Disable both Stream Matching and Stream Checking
4. Sports channels will no longer appear in other tabs

#### Example 2: Enable Quality Checking Only
1. Find a channel group
2. Set Stream Matching to "Disabled"
3. Set Stream Checking to "Enabled"
4. Channels will appear in tabs and be quality checked, but won't participate in stream matching

#### Example 3: Bulk Management
1. Quickly review all groups at once
2. Enable/disable settings for multiple groups
3. Changes apply immediately to all channels in each group

### Technical Details

- **Persistence**: Group settings are saved to disk and survive restarts
- **Real-time Updates**: Changes apply immediately via API
- **No Channel Modification**: Group settings don't modify individual channel configurations
- **Inheritance**: Individual channel settings take precedence over group settings

## Channel Order Tab Features

### Overview
Organize your channels using an intuitive drag-and-drop interface with multiple sorting options.

### Features

1. **Drag and Drop Reordering**: Click and drag channels to change their order
2. **Sort Options**:
   - Custom Order: Manual drag-and-drop arrangement
   - Channel Number: Sort by channel number (1, 2, 3...)
   - Name (A-Z): Alphabetical sorting
   - ID: Sort by internal channel ID
3. **Visible Channel Filtering**: Only shows channels from groups with enabled settings
4. **Unsaved Changes Detection**: Shows alert when you have pending changes
5. **Save/Reset Actions**: Easily save or discard your ordering changes

### Workflow

1. Switch to the Channel Order tab
2. Use the sort dropdown to organize channels by your preferred method
3. Optionally, drag and drop channels to fine-tune the order
4. Click "Save Order" to persist your changes
5. Or click "Reset" to discard changes

## Backwards Compatibility

All existing functionality is preserved:
- ✅ Add new patterns
- ✅ Edit existing patterns  
- ✅ Delete patterns
- ✅ Test patterns against live streams
- ✅ Enable/disable patterns
- ✅ Manage channel groups
- ✅ Reorder channels

## Future Enhancements

Potential improvements:
- Advanced filters (pattern count ranges, last modified date)
- Bulk pattern deletion across multiple channels
- Pattern templates library for common use cases
- Export/import group settings
- Per-group regex templates
- Channel group creation and editing from UI
- Pattern validation and suggestions
- Regex pattern testing against historical stream data
