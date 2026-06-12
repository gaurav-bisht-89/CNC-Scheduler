import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px

st.set_page_config(page_title="CNC Control Center", layout="wide")

# =====================================================================
# CONFIGURATION: DIRECT CLOUD SYNCHRONIZATION LINK
# =====================================================================
NEW_SHEET_ID = "1iuFMQHJssHz4z0_zW-HQ6gMTAnQiRiqB6m2_hboiOFc"

def fetch_with_fallback(sheet_id, expected_name):
    variations = [
        expected_name,
        expected_name.lower(),
        expected_name.upper(),
        expected_name.replace("Master", " Master").replace("Calendar", " Calendar")
    ]
    
    for name in variations:
        try:
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={name}"
            df = pd.read_csv(csv_url)
            if df.empty:
                continue
            
            df.columns = df.columns.str.strip()
            for col in df.select_dtypes(include='object').columns:
                df[col] = df[col].str.strip()
            return df
        except:
            continue
            
    raise ValueError(f"Could not find a valid tab named '{expected_name}' (or variations) in your Sheet.")

@st.cache_data(ttl=2)
def run_core_scheduler_engine():
    orders = fetch_with_fallback(NEW_SHEET_ID, "Orders")
    routing = fetch_with_fallback(NEW_SHEET_ID, "RoutingMaster")
    machines = fetch_with_fallback(NEW_SHEET_ID, "MachineMaster")
    calendar = fetch_with_fallback(NEW_SHEET_ID, "WorkingCalendar")
    maintenance = fetch_with_fallback(NEW_SHEET_ID, "Maintenance")

    # Column Mapping Alignments for Parts
    for df in [orders, routing]:
        if 'Part ID' in df.columns and 'Part No.' not in df.columns:
            df.rename(columns={'Part ID': 'Part No.'}, inplace=True)

    # Column Mapping Alignments for Machine Names
    possible_name_cols = ['Machine Name', 'Machine', 'Description', 'Asset Name', 'Name']
    for col in possible_name_cols:
        if col in machines.columns and col != 'Machine Name':
            machines.rename(columns={col: 'Machine Name'}, inplace=True)
            break

    # OEE Text String to Float Processor
    if 'OEE' in machines.columns:
        machines['OEE'] = machines['OEE'].astype(str).str.replace('%', '', regex=False).str.strip()
        machines['OEE'] = pd.to_numeric(machines['OEE'], errors='coerce')
        machines['OEE'] = machines['OEE'].apply(lambda x: x / 100.0 if x > 1.0 else x)
        machines['OEE'] = machines['OEE'].fillna(0.70)

    calendar['Date'] = pd.to_datetime(calendar['Date'], errors='coerce')
    maintenance['Start Date'] = pd.to_datetime(maintenance['Start Date'], errors='coerce')
    maintenance['End Date'] = pd.to_datetime(maintenance['End Date'], errors='coerce')
    orders['Due Date'] = pd.to_datetime(orders['Due Date'], errors='coerce')
    orders['Start Date'] = pd.to_datetime(orders['Start Date'], errors='coerce')

    valid_calendar = calendar[calendar['Date'].notna()].sort_values('Date')
    shop_dates = sorted(valid_calendar['Date'].unique())
    master_part_list = sorted(orders['Part No.'].dropna().unique())
    master_machine_list = sorted(list(set(machines['Machine ID'].dropna().unique()).union(set(routing['Machine ID'].dropna().unique()))))

    capacity_matrix = pd.DataFrame(0.0, index=master_machine_list, columns=shop_dates)
    baseline_capacities, total_available_shop_minutes = {}, {}

    for m_id in master_machine_list:
        mach_info = machines[machines['Machine ID'] == m_id]
        shifts_val = int(mach_info['Shifts'].values[0]) if not mach_info.empty and 'Shifts' in mach_info.columns else 2
        oee_val = float(mach_info['OEE'].values[0]) if not mach_info.empty and 'OEE' in mach_info.columns else 0.70
        
        daily_capacity = (shifts_val * 8 * 60 * oee_val)
        baseline_capacities[m_id] = daily_capacity
        total_minutes = 0.0

        for dt in shop_dates:
            day_work_check = valid_calendar[valid_calendar['Date'] == dt]
            if not day_work_check.empty and 'Working' in day_work_check.columns:
                if str(day_work_check['Working'].values[0]).strip().upper() == 'N':
                    capacity_matrix.loc[m_id, dt] = -1.0
                    continue
                    
            maint = maintenance[(maintenance['Machine ID'] == m_id) & (dt >= maintenance['Start Date']) & (dt <= maintenance['End Date'])]
            if not maint.empty:
                capacity_matrix.loc[m_id, dt] = -2.0
                continue
                
            capacity_matrix.loc[m_id, dt] = daily_capacity
            total_minutes += daily_capacity
        total_available_shop_minutes[m_id] = total_minutes

    if 'Setup No.' in routing.columns:
        routing['Setup_Num'] = routing['Setup No.'].astype(str).str.extract(r'(\d+)').fillna(1).astype(int)
    else:
        routing['Setup_Num'] = 1

    time_col = 'Time Per Part (min)' if 'Time Per Part (min)' in routing.columns else ('Time Per Part' if 'Time Per Part' in routing.columns else routing.columns[-1])
    orders_processing = orders.dropna(subset=['Part No.', 'Qty']).sort_values(by=['Priority', 'Due Date']).copy()
    all_operational_tasks = []

    for idx, order in orders_processing.iterrows():
        part_steps = routing[routing['Part No.'] == order['Part No.']].sort_values(by='Setup_Num')
        if part_steps.empty:
            continue
            
        total_part_cycle_time = sum([
            float(step[time_col])/float(step['Batch Size']) if 'Batch Size' in step and float(step['Batch Size'])>0 else float(step[time_col]) 
            for _, step in part_steps.iterrows()
        ])
        
        for op_idx, (_, step) in enumerate(part_steps.iterrows()):
            b_size = float(step['Batch Size']) if 'Batch Size' in step and pd.notna(step['Batch Size']) and float(step['Batch Size'])>0 else 1.0
            unit_time = float(step[time_col]) / b_size
            rel_date = order['Start Date'] if 'Start Date' in order and pd.notna(order['Start Date']) else shop_dates[0]
            
            all_operational_tasks.append({
                'Job Index': idx, 'Order ID': order['Order ID'], 'Part No.': order['Part No.'],
                'Part Name': str(order['Part Name']).strip() if 'Part Name' in order else 'Part Component', 
                'Setup No.': step['Setup No.'] if 'Setup No.' in step else f"Setup {op_idx+1}", 
                'Setup Name': step['Setup Name'] if 'Setup Name' in step else 'Machining Op', 
                'Primary Machine ID': step['Machine ID'], 'Priority': order['Priority'] if 'Priority' in order else 2, 
                'Due Date': order['Due Date'], 'Order Qty': order['Qty'], 'Op Index': op_idx, 'Unit Time': unit_time, 
                'Remaining Minutes to Schedule': order['Qty'] * unit_time,
                'Is Final Op': (op_idx == len(part_steps) - 1), 'Total Part Cycle Time': total_part_cycle_time, 
                'Earliest Start Date': pd.to_datetime(rel_date)
            })

    interchangeable_groups = {
        'M001': ['M001', 'M002', 'M003'], 'M002': ['M001', 'M002', 'M003'], 'M003': ['M001', 'M002', 'M003'],
        'M005': ['M005', 'M006'], 'M006': ['M005', 'M006']
    }

    scheduled_operations_log = []
    for current_date in shop_dates:
        active_pool = [t for t in all_operational_tasks if t['Remaining Minutes to Schedule'] > 0 and t['Earliest Start Date'] <= current_date]
        if not active_pool: 
            continue
        active_pool.sort(key=lambda x: (x['Priority'], x['Due Date'], x['Op Index']))
        
        for task in active_pool:
            primary_mach = task['Primary Machine ID']
            candidate_machines = interchangeable_groups.get(primary_mach, [primary_mach])
            
            if task['Op Index'] > 0:
                prev_task = [t for t in all_operational_tasks if t['Job Index'] == task['Job Index'] and t['Op Index'] == task['Op Index'] - 1][0]
                if prev_task['Remaining Minutes to Schedule'] > 0: 
                    continue
                    
            selected_mach, max_available_time = None, 0.0
            for mach in candidate_machines:
                if mach not in capacity_matrix.index:
                    continue
                space_today = capacity_matrix.loc[mach, current_date]
                if space_today > max_available_time:
                    max_available_time = space_today
                    selected_mach = mach
                    
            if selected_mach is None or max_available_time <= 0: 
                continue
                
            minutes_to_allocate = min(task['Remaining Minutes to Schedule'], max_available_time)
            capacity_matrix.loc[selected_mach, current_date] -= minutes_to_allocate
            task['Remaining Minutes to Schedule'] -= minutes_to_allocate
            
            scheduled_operations_log.append({
                'Job Index': task['Job Index'], 'Order ID': task['Order ID'], 'Part No.': task['Part No.'],
                'Part Name': task['Part Name'], 'Setup No.': task['Setup No.'], 'Setup Name': task['Setup Name'],
                'Machine ID': selected_mach, 'Scheduled Date': current_date, 'Minutes Allocated': minutes_to_allocate,
                'Pieces Today': minutes_to_allocate / task['Unit Time'], 'Order Qty': task['Order Qty'], 'Due Date': task['Due Date'],
                'Is Final Op': task['Is Final Op'], 'Op Time Per Part': task['Unit Time'], 'Total Part Cycle Time': task['Total Part Cycle Time']
            })

    return pd.DataFrame(scheduled_operations_log), capacity_matrix, baseline_capacities, total_available_shop_minutes, shop_dates, master_part_list, master_machine_list, machines, orders_processing

# Run Data Core
try:
    schedule_df, capacity_matrix, baseline_capacities, total_available_shop_minutes, shop_dates, master_part_list, master_machine_list, machines_master, orders_processing = run_core_scheduler_engine()
except Exception as e:
    st.error("❌ **Detailed Sheet Connection Breakdown**")
    st.code(str(e))
    st.stop()

# Headers
st.title("🏭 CNC SHOP FLOOR OPERATIONS CONTROL CENTER")
st.caption("Live Cloud-Controlled Finite Capacity Shop Floor Scheduler Platform")
st.write("---")

selected_view = st.sidebar.radio("Navigation Control Panel:", ["📦 Component Delivery Flow Chart", "📊 Capacity Utilization Profile", "📋 Executive Milestone Reports"])

# Fixed Target Boundaries matching original requirements
deadline_1 = pd.to_datetime('2026-06-25')
deadline_2 = pd.to_datetime('2026-07-05')

# =====================================================================
# VIEW 1: REPLICATED CUMULATIVE TIME-SERIES PRODUCTION LINE GRAPH
# =====================================================================
if selected_view == "📦 Component Delivery Flow Chart":
    st.subheader("📦 Part-Wise Logistics Accumulation Curves")
    
    # Let user pick which component to track exactly like the original color lanes
    selected_part = st.selectbox("Select Component to View Progress Link:", master_part_list)
    
    # Filter logistics logging rows for this specific component
    part_runs = schedule_df[schedule_df['Part No.'] == selected_part] if not schedule_df.empty else pd.DataFrame()
    matched_orders = orders_processing[orders_processing['Part No.'] == selected_part]
    total_needed = int(matched_orders['Qty'].sum()) if not matched_orders.empty else 0
    
    # Create daily accumulated arrays
    cum_data = []
    running_total = 0.0
    
    for dt in shop_dates:
        # Pull outputs completed on this exact shift date
        day_outputs = part_runs[(part_runs['Scheduled Date'] == dt) & (part_runs['Is Final Op'] == True)]
        if not day_outputs.empty:
            running_total += day_outputs['Pieces Today'].sum()
            
        cum_data.append({
            'Date': dt,
            'Accumulated Pieces': min(running_total, total_needed),
            'Target Cap': total_needed
        })
        
    df_cum = pd.DataFrame(cum_data)
    
    # Build Line Graph Replicating 'Plot2.png'
    fig_part = go.Figure()
    
    # 1. Main Accumulated Output Shaded Area Line
    fig_part.add_trace(go.Scatter(
        x=df_cum['Date'], y=df_cum['Accumulated Pieces'],
        mode='lines+markers', name='Scheduled Cumulative Yield',
        line=dict(color='#1E88E5', width=3, shape='hv'), # 'hv' creates step-staircase effect
        fill='tozeroy', fillcolor='rgba(30, 136, 229, 0.15)'
    ))
    
    # 2. Top Ceiling Cap Target Line
    fig_part.add_trace(go.Scatter(
        x=df_cum['Date'], y=df_cum['Target Cap'],
        mode='lines', name='Total Order Target',
        line=dict(color='#E53935', width=2, dash='dash')
    ))
    
    # 3. Vertical Target Milestone Boundary Lines (June 25 and July 5)
    fig_part.add_vline(x=deadline_1.timestamp() * 1000, line_width=2, line_dash="dash", line_color="#43A047", 
                       annotation_text="June 25 Milestone", annotation_position="top left")
    fig_part.add_vline(x=deadline_2.timestamp() * 1000, line_width=2, line_dash="dash", line_color="#000000", 
                       annotation_text="July 5 Target Complete", annotation_position="top left")

    fig_part.update_layout(
        xaxis_title="Timeline Calendar",
        yaxis_title="Accumulated Completed Pieces (pcs)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=40, t=40, b=40),
        height=550,
        template="plotly_white"
    )
    
    st.plotly_chart(fig_part, use_container_width=True)
    
    # Dynamic Metric Cards below the graph
    current_yield = int(round(running_total))
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Order Quantity Required", f"{total_needed} pcs")
    c2.metric("Scheduled Delivery Yield", f"{min(current_yield, total_needed)} pcs")
    c3.metric("Remaining Shortfall Balance", f"{max(0, total_needed - current_yield)} pcs", delta=None, delta_color="inverse")

# VIEW 2: MACHINE CAPACITY BAR CHART
elif selected_view == "📊 Capacity Utilization Profile":
    st.subheader("📊 Post-Optimization Machine Load Profiles")
    balanced_load_data = []
    for m_id in master_machine_list:
        actual_min = schedule_df[schedule_df['Machine ID'] == m_id]['Minutes Allocated'].sum() if not schedule_df.empty else 0.0
        available_min = total_available_shop_minutes.get(m_id, 0.0)
        true_util = (actual_min / available_min * 100) if available_min > 0 else 0.0
        
        if 'Machine Name' in machines_master.columns:
            m_name_lookup = machines_master[machines_master['Machine ID'] == m_id]['Machine Name']
            m_name = m_name_lookup.values[0] if not m_name_lookup.empty else m_id
        else:
            m_name = m_id
            
        balanced_load_data.append({
            'Machine ID': m_id, 'Machine Name': m_name, 'Utilization (%)': round(true_util, 1),
            'Workload (Hrs)': round(actual_min / 60, 1), 'Capacity (Hrs)': round(available_min / 60, 1)
        })
    
    df_load = pd.DataFrame(balanced_load_data)
    fig_load = px.bar(df_load, x='Machine ID', y='Utilization (%)', text='Utilization (%)', color='Utilization (%)',
        custom_data=['Machine Name', 'Workload (Hrs)', 'Capacity (Hrs)'], color_continuous_scale='YlOrRd')
    fig_load.update_traces(texttemplate='%{text}%', textposition='outside',
        hovertemplate="<b>Machine ID:</b> %{x}<br><b>Name:</b> %{customdata[0]}<br><b>Scheduled:</b> %{customdata[1]} Hrs<br><b>Capacity:</b> %{customdata[2]} Hrs<extra></extra>")
    fig_load.add_hline(y=100.0, line_dash="dash", line_color="red")
    fig_load.update_layout(yaxis=dict(tickmode='linear', dtick=10, range=[0, max(df_load['Utilization (%)'].max()+15, 120)]), template="plotly_white")
    st.plotly_chart(fig_load, use_container_width=True)

# VIEW 3: SHIPMENT MANAGEMENT SUMMARY TABLES
else:
    st.subheader("📋 Executive Performance & Milestone Outcomes")
    m1_summary, m2_summary = [], []
    for p in master_part_list:
        p_name = orders_processing[orders_processing['Part No.'] == p]['Part Name'].iloc[0].strip()
        
        # June 25 Metrics Extraction
        t1 = int(orders_processing[(orders_processing['Part No.'] == p) & (orders_processing['Due Date'] <= deadline_1)]['Qty'].sum())
        p_runs1 = schedule_df[(schedule_df['Part No.'] == p) & (schedule_df['Scheduled Date'] <= deadline_1)] if not schedule_df.empty else pd.DataFrame()
        comp1 = min(t1, p_runs1['Minutes Allocated'].sum() / p_runs1['Total Part Cycle Time'].iloc[0]) if not p_runs1.empty else 0.0
        sf1 = max(0, t1-int(round(comp1)))
        m1_summary.append({'Part ID': p, 'Part Name': p_name, 'Target Order Qty': f"{t1} pcs", 'Scheduled Output': f"{int(round(comp1))} pcs", 'Shortfall Carryover': f"{sf1} pcs", 'Status': '✅ ON-TRACK' if sf1==0 else '⚠️ SHORTFALL'})
        
        # July 5 Metrics Extraction
        t2 = int(orders_processing[(orders_processing['Part No.'] == p) & (orders_processing['Due Date'] <= deadline_2)]['Qty'].sum())
        p_runs2 = schedule_df[(schedule_df['Part No.'] == p) & (schedule_df['Scheduled Date'] <= deadline_2)] if not schedule_df.empty else pd.DataFrame()
        comp2 = min(t2, p_runs2['Minutes Allocated'].sum() / p_runs2['Total Part Cycle Time'].iloc[0]) if not p_runs2.empty else 0.0
        sf2 = max(0, t2-int(round(comp2)))
        m2_summary.append({'Part ID': p, 'Part Name': p_name, 'Target Order Qty': f"{t2} pcs", 'Scheduled Output': f"{int(round(comp2))} pcs", 'Shortfall Carryover': f"{sf2} pcs", 'Status': '✅ ON-TRACK' if sf2==0 else '⚠️ SHORTFALL'})

    st.markdown("### 🔵 June 25th Shipping Milestone Delivery Table")
    st.dataframe(pd.DataFrame(m1_summary), use_container_width=True, hide_index=True)
    st.markdown("### 🟢 July 5th Consolidated Milestone Delivery Table")
    st.dataframe(pd.DataFrame(m2_summary), use_container_width=True, hide_index=True)
