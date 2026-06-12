import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px

st.set_page_config(page_title="CNC Control Center", layout="wide")

# =====================================================================
# CONFIGURATION: LINKED DIRECTLY TO YOUR GOOGLE SHEET
# =====================================================================
NEW_SHEET_ID = "1iuFMQHJssHz4z0_zW-HQ6gMTAnQiRiqB6m2_hboiOFc"

@st.cache_data(ttl=5) # Checks for sheet updates every 5 seconds
def load_clean_cloud_data(sheet_id, sheet_name):
    csv_url = f"https://docs.google.com/spreadsheets/d/1iuFMQHJssHz4z0_zW-HQ6gMTAnQiRiqB6m2_hboiOFc/edit?usp=sharing"
    df = pd.read_csv(csv_url)
    
    # Strip hidden whitespaces from columns and row data strings
    df.columns = df.columns.str.strip()
    for col in df.select_dtypes(include='object').columns:
        df[col] = df[col].str.strip()
    return df

@st.cache_data(ttl=5)
def run_core_scheduler_engine():
    orders = load_clean_cloud_data(NEW_SHEET_ID, "Orders")
    routing = load_clean_cloud_data(NEW_SHEET_ID, "RoutingMaster")
    machines = load_clean_cloud_data(NEW_SHEET_ID, "MachineMaster")
    calendar = load_clean_cloud_data(NEW_SHEET_ID, "WorkingCalendar")
    maintenance = load_clean_cloud_data(NEW_SHEET_ID, "Maintenance")

    # =====================================================================
    # DYNAMIC COLUMN NAME NORMALIZATION (FIXES KEYERROR: 'PART NO.')
    # =====================================================================
    # Checks if sheets use 'Part ID' and changes it to 'Part No.' in the background
    for df in [orders, routing]:
        if 'Part ID' in df.columns and 'Part No.' not in df.columns:
            df.rename(columns={'Part ID': 'Part No.'}, inplace=True)

    calendar['Date'] = pd.to_datetime(calendar['Date'], errors='coerce')
    maintenance['Start Date'] = pd.to_datetime(maintenance['Start Date'], errors='coerce')
    maintenance['End Date'] = pd.to_datetime(maintenance['End Date'], errors='coerce')
    orders['Due Date'] = pd.to_datetime(orders['Due Date'], errors='coerce')
    orders['Start Date'] = pd.to_datetime(orders['Start Date'], errors='coerce')

    calendar = calendar[(calendar['Date'] >= pd.to_datetime('2026-06-06')) & (calendar['Date'] < pd.to_datetime('2026-07-06'))]
    shop_dates = sorted(calendar['Date'].unique())
    master_part_list = sorted(orders['Part No.'].unique())
    master_machine_list = sorted(list(set(machines['Machine ID'].unique()).union(set(routing['Machine ID'].unique()))))

    capacity_matrix = pd.DataFrame(0.0, index=master_machine_list, columns=shop_dates)
    baseline_capacities, total_available_shop_minutes = {}, {}

    for m_id in master_machine_list:
        mach_info = machines[machines['Machine ID'] == m_id]
        daily_capacity = (int(mach_info['Shifts'].values[0]) * 8 * 60 * float(mach_info['OEE'].values[0])) if not mach_info.empty else (2 * 8 * 60 * 0.7)
        baseline_capacities[m_id] = daily_capacity
        total_minutes = 0.0

        for dt in shop_dates:
            if calendar.loc[calendar['Date'] == dt, 'Working'].values[0] == 'N':
                capacity_matrix.loc[m_id, dt] = -1.0
                continue
            maint = maintenance[(maintenance['Machine ID'] == m_id) & (dt >= maintenance['Start Date']) & (dt <= maintenance['End Date'])]
            if not maint.empty:
                capacity_matrix.loc[m_id, dt] = -2.0
                continue
            capacity_matrix.loc[m_id, dt] = daily_capacity
            total_minutes += daily_capacity
        total_available_shop_minutes[m_id] = total_minutes

    routing['Setup_Num'] = routing['Setup No.'].str.extract(r'(\d+)').astype(int)
    time_col = 'Time Per Part (min)' if 'Time Per Part (min)' in routing.columns else 'Time Per Part'
    orders_processing = orders.sort_values(by=['Priority', 'Due Date']).copy()
    all_operational_tasks = []

    for idx, order in orders_processing.iterrows():
        part_steps = routing[routing['Part No.'] == order['Part No.']].sort_values(by='Setup_Num')
        total_part_cycle_time = sum([float(step[time_col])/float(step['Batch Size']) if float(step['Batch Size'])>0 else float(step[time_col]) for _, step in part_steps.iterrows()])
        
        for op_idx, (_, step) in enumerate(part_steps.iterrows()):
            b_size = float(step['Batch Size']) if pd.notna(step['Batch Size']) and float(step['Batch Size'])>0 else 1.0
            unit_time = float(step[time_col]) / b_size
            all_operational_tasks.append({
                'Job Index': idx, 'Order ID': order['Order ID'], 'Part No.': order['Part No.'],
                'Part Name': order['Part Name'].strip(), 'Setup No.': step['Setup No.'],
                'Setup Name': step['Setup Name'], 'Primary Machine ID': step['Machine ID'],
                'Priority': order['Priority'], 'Due Date': order['Due Date'], 'Order Qty': order['Qty'],
                'Op Index': op_idx, 'Unit Time': unit_time, 'Remaining Minutes to Schedule': order['Qty'] * unit_time,
                'Is Final Op': (step['Setup No.'] == part_steps['Setup No.'].iloc[-1]),
                'Total Part Cycle Time': total_part_cycle_time, 'Earliest Start Date': order['Start Date']
            })

    interchangeable_groups = {
        'M001': ['M001', 'M002', 'M003'], 'M002': ['M001', 'M002', 'M003'], 'M003': ['M001', 'M002', 'M003'],
        'M005': ['M005', 'M006'], 'M006': ['M005', 'M006']
    }

    scheduled_operations_log = []
    for current_date in shop_dates:
        active_pool = [t for t in all_operational_tasks if t['Remaining Minutes to Schedule'] > 0 and t['Earliest Start Date'] <= current_date]
        if not active_pool: continue
        active_pool.sort(key=lambda x: (x['Priority'], x['Due Date'], x['Op Index']))
        
        for task in active_pool:
            primary_mach = task['Primary Machine ID']
            candidate_machines = interchangeable_groups.get(primary_mach, [primary_mach])
            if task['Op Index'] > 0:
                prev_task = [t for t in all_operational_tasks if t['Job Index'] == task['Job Index'] and t['Op Index'] == task['Op Index'] - 1][0]
                if prev_task['Remaining Minutes to Schedule'] > 0: continue
                    
            selected_mach, max_available_time = None, 0.0
            for mach in candidate_machines:
                space_today = capacity_matrix.loc[mach, current_date]
                if space_today > max_available_time:
                    max_available_time = space_today
                    selected_mach = mach
                    
            if selected_mach is None or max_available_time <= 0: continue
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

# Run Engine
try:
    schedule_df, capacity_matrix, baseline_capacities, total_available_shop_minutes, shop_dates, master_part_list, master_machine_list, machines_master, orders_processing = run_core_scheduler_engine()
except Exception as e:
    st.error(f"❌ **Cloud Sheet Syncing Error:** {e}")
    st.info("Ensure all tabs (Orders, RoutingMaster, MachineMaster, WorkingCalendar, Maintenance) match the required configurations exactly.")
    st.stop()

# Header
st.markdown("""
    <div style="background-color:#1E293B; padding:20px; border-radius:10px; margin-bottom:25px;">
        <h1 style="color:white; margin:0; font-family:'Segoe UI',sans-serif;">🏭 CNC SHOP FLOOR OPERATIONS CONTROL CENTER</h1>
        <p style="color:#94A3B8; margin:5px 0 0 0;">Real-Time Finite Capacity Cross-Routing Scheduler Dashboard</p>
    </div>
""", unsafe_allowed_html=True)

selected_view = st.sidebar.radio("Navigation Control Panel:", ["📊 Capacity Utilization Profile", "📦 Component Flow Roadmap", "📋 Executive Milestone Reports"])
deadline_1, deadline_2 = pd.to_datetime('2026-06-25'), pd.to_datetime('2026-07-05')

# VIEW 1: BAR CHART
if selected_view == "📊 Capacity Utilization Profile":
    st.subheader("📊 Post-Optimization Machine Load Profiles")
    balanced_load_data = []
    for m_id in master_machine_list:
        actual_min = schedule_df[schedule_df['Machine ID'] == m_id]['Minutes Allocated'].sum() if not schedule_df.empty else 0.0
        available_min = total_available_shop_minutes.get(m_id, 0.0)
        true_util = (actual_min / available_min * 100) if available_min > 0 else 0.0
        balanced_load_data.append({'Machine ID': m_id, 'Utilization (%)': round(true_util, 1)})
    
    df_load = pd.DataFrame(balanced_load_data)
    fig_load = px.bar(df_load, x='Machine ID', y='Utilization (%)', text='Utilization (%)', color='Utilization (%)', color_continuous_scale='YlOrRd')
    fig_load.add_hline(y=100.0, line_dash="dash", line_color="red")
    fig_load.update_layout(yaxis=dict(tickmode='linear', dtick=10, range=[0, max(df_load['Utilization (%)'].max()+15, 120)]), template="plotly_white")
    st.plotly_chart(fig_load, use_container_width=True)

# VIEW 2: HEATMAP
elif selected_view == "📦 Component Flow Roadmap":
    st.subheader("📦 Component Delivery Tracking Channels")
    color_grid_part = np.zeros((len(master_part_list), len(shop_dates)))
    hover_text_part = np.empty((len(master_part_list), len(shop_dates)), dtype=object)
    date_labels = [d.strftime('%b-%d') for d in shop_dates]

    for idx, p_no in enumerate(master_part_list):
        matched_orders = orders_processing[orders_processing['Part No.'] == p_no]
        for jdx, dt in enumerate(shop_dates):
            if dt.weekday() == 6:
                color_grid_part[idx, jdx] = -1
                hover_text_part[idx, jdx] = "Sunday Rest Window"
                continue
            day_jobs = schedule_df[(schedule_df['Part No.'] == p_no) & (schedule_df['Scheduled Date'] == dt)]
            past_and_today = schedule_df[(schedule_df['Part No.'] == p_no) & (schedule_df['Scheduled Date'] <= dt)]
            target_qty = int(matched_orders[matched_orders['Due Date'] <= deadline_1]['Qty'].sum()) if dt <= deadline_1 else int(matched_orders['Qty'].sum())
            finished = min(target_qty, past_and_today['Minutes Allocated'].sum() / past_and_today['Total Part Cycle Time'].iloc[0]) if not past_and_today.empty else 0.0
            color_grid_part[idx, jdx] = 1 if not day_jobs.empty else (0 if finished >= target_qty else 3)
            hover_text_part[idx, jdx] = f"Part No: {p_no}<br>Yield Status: {finished:.1f} / {target_qty} Total Pieces Scheduled"

    fig_part = go.Figure(data=go.Heatmap(z=color_grid_part, x=date_labels, y=master_part_list, text=hover_text_part, hoverinfo='text', colorscale=[[0.0,'#FFB4B4'], [0.25,'#5A646E'], [0.5,'#F58C00'], [1.0,'#FFE13B']], showscale=False, xgap=2, ygap=2))
    st.plotly_chart(fig_part, use_container_width=True)

# VIEW 3: SUMMARY TABLES
else:
    st.subheader("📋 Executive Performance & Milestone Outcomes")
    m1_summary, m2_summary = [], []
    for p in master_part_list:
        p_name = orders_processing[orders_processing['Part No.'] == p]['Part Name'].iloc[0].strip()
        
        t1 = int(orders_processing[(orders_processing['Part No.'] == p) & (orders_processing['Due Date'] <= deadline_1)]['Qty'].sum())
        p_runs1 = schedule_df[(schedule_df['Part No.'] == p) & (schedule_df['Scheduled Date'] <= deadline_1)] if not schedule_df.empty else pd.DataFrame()
        comp1 = min(t1, p_runs1['Minutes Allocated'].sum() / p_runs1['Total Part Cycle Time'].iloc[0]) if not p_runs1.empty else 0.0
        sf1 = max(0, t1-int(round(comp1)))
        m1_summary.append({'Part ID': p, 'Part Name': p_name, 'Target Order Qty': f"{t1} pcs", 'Scheduled Output': f"{int(round(comp1))} pcs", 'Shortfall Carryover': f"{sf1} pcs", 'Status': '✅ ON-TRACK' if sf1==0 else '⚠️ SHORTFALL'})
        
        t2 = int(orders_processing[(orders_processing['Part No.'] == p) & (orders_processing['Due Date'] <= deadline_2)]['Qty'].sum())
        p_runs2 = schedule_df[(schedule_df['Part No.'] == p) & (schedule_df['Scheduled Date'] <= deadline_2)] if not schedule_df.empty else pd.DataFrame()
        comp2 = min(t2, p_runs2['Minutes Allocated'].sum() / p_runs2['Total Part Cycle Time'].iloc[0]) if not p_runs2.empty else 0.0
        sf2 = max(0, t2-int(round(comp2)))
        m2_summary.append({'Part ID': p, 'Part Name': p_name, 'Target Order Qty': f"{t2} pcs", 'Scheduled Output': f"{int(round(comp2))} pcs", 'Shortfall Carryover': f"{sf2} pcs", 'Status': '✅ ON-TRACK' if sf2==0 else '⚠️ SHORTFALL'})

    st.markdown("### 🔵 June 25th Shipping Milestone Delivery Table")
    st.dataframe(pd.DataFrame(m1_summary), use_container_width=True, hide_index=True)
    st.markdown("### 🟢 July 5th Consolidated Milestone Delivery Table")
    st.dataframe(pd.DataFrame(m2_summary), use_container_width=True, hide_index=True)