from dash import Dash, html, dcc, Output, Input, State, clientside_callback
import dash

app = Dash(__name__)

app.layout = html.Div([
    html.Button("Add Line", id="add-line-btn"),
    html.Div(
        id="content-container",
        style={'height': '200px', 'overflowY': 'auto', 'border': '1px solid black'}
    ),
    dcc.Store(id="scroll-state", data={"at_bottom": True}),
    dcc.Interval(id="update-interval", interval=500, n_intervals=0),
    dcc.Store(id="dummy-scroll-output")
])

# Python callback to add new content
@app.callback(
    Output("content-container", "children"),
    Input("add-line-btn", "n_clicks"),
    State("content-container", "children"),
)
def add_line(n_clicks, current_content):
    current_content = current_content or []
    if n_clicks:
        current_content.append(html.P(f"Line {n_clicks}"))
    return current_content

# Clientside JS to detect scroll position
app.clientside_callback(
    """
    function(n_intervals) {
        var el = document.getElementById('content-container');
        if(el){
            var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
            return {"at_bottom": atBottom};
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("scroll-state", "data"),
    Input("update-interval", "n_intervals")
)

# Clientside JS to scroll only if at_bottom
app.clientside_callback(
    """
    function(children, scroll_data) {
        if(!children) return window.dash_clientside.no_update;
        var el = document.getElementById('content-container');
        if(el && scroll_data && scroll_data.at_bottom){
            el.scrollTop = el.scrollHeight;
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("dummy-scroll-output", "data"),  # dummy output
    Input("content-container", "children"),
    State("scroll-state", "data")
)

if __name__ == '__main__':
    app.run(debug=True)


# from dash import Dash, html, Input, Output, clientside_callback, callback
# import time

# app = Dash(__name__)

# app.layout = html.Div([
#     html.Button("Add Content", id="add-content-button"),
#     html.Div(id="content-container", style={'height': '300px', 'overflowY': 'scroll', 'border': '1px solid black'}),
# ])

# @callback(
#     Output("content-container", "children"),
#     Input("add-content-button", "n_clicks"),
#     prevent_initial_call=True
# )
# def add_content(n_clicks):
#     if n_clicks is None:
#         return []
    
#     current_content = []
#     if n_clicks > 1:
#         # Simulate adding more content
#         for i in range(n_clicks):
#             current_content.append(html.P(f"Line {i+1} of new content."))
#     else:
#         current_content.append(html.P("Initial content."))
        
#     return current_content

# clientside_callback(
#     """
#     function(n_clicks) {
#         if (n_clicks) {
#             var objDiv = document.getElementById('content-container');
#             objDiv.scrollTop = objDiv.scrollHeight;
#         }
#         return window.dash_clientside.no_update;
#     }
#     """,
#     Output("content-container", "data-dummy"), # Dummy output to trigger the callback
#     Input("add-content-button", "n_clicks"),
#     prevent_initial_call=True
# )

# if __name__ == '__main__':
#     app.run(debug=True)
