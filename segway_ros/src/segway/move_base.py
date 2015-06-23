"""--------------------------------------------------------------------
COPYRIGHT 2014 Stanley Innovation Inc.

Software License Agreement:

The software supplied herewith by Stanley Innovation Inc. (the "Company") 
for its licensed Segway RMP Robotic Platforms is intended and supplied to you, 
the Company's customer, for use solely and exclusively with Stanley Innovation 
products. The software is owned by the Company and/or its supplier, and is 
protected under applicable copyright laws.  All rights are reserved. Any use in 
violation of the foregoing restrictions may subject the user to criminal 
sanctions under applicable laws, as well as to civil liability for the 
breach of the terms and conditions of this license. The Company may 
immediately terminate this Agreement upon your use of the software with 
any products that are not Stanley Innovation products.

The software was written using Python programming language.  Your use 
of the software is therefore subject to the terms and conditions of the 
OSI- approved open source license viewable at http://www.python.org/.  
You are solely responsible for ensuring your compliance with the Python 
open source license.

You shall indemnify, defend and hold the Company harmless from any claims, 
demands, liabilities or expenses, including reasonable attorneys fees, incurred 
by the Company as a result of any claim or proceeding against the Company 
arising out of or based upon: 

(i) The combination, operation or use of the software by you with any hardware, 
    products, programs or data not supplied or approved in writing by the Company, 
    if such claim or proceeding would have been avoided but for such combination, 
    operation or use.
 
(ii) The modification of the software by or on behalf of you 

(iii) Your use of the software.

 THIS SOFTWARE IS PROVIDED IN AN "AS IS" CONDITION. NO WARRANTIES,
 WHETHER EXPRESS, IMPLIED OR STATUTORY, INCLUDING, BUT NOT LIMITED
 TO, IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
 PARTICULAR PURPOSE APPLY TO THIS SOFTWARE. THE COMPANY SHALL NOT,
 IN ANY CIRCUMSTANCES, BE LIABLE FOR SPECIAL, INCIDENTAL OR
 CONSEQUENTIAL DAMAGES, FOR ANY REASON WHATSOEVER.
 
 \file   rmp_comm.py

 \brief  runs the driver

 \Platform: Linux/ROS Indigo
--------------------------------------------------------------------"""
import rospy
import tf
import actionlib
from system_defines import *
from actionlib_msgs.msg import *
from segway_msgs.msg import *
from geometry_msgs.msg import Pose, PoseStamped, PoseWithCovarianceStamped, Point, Quaternion, Twist
from move_base_msgs.msg import *
from std_msgs.msg import Bool
from math import pow, sqrt
from system_defines import *

class SegwayMoveBase():
    def __init__(self):
    
        """
        Initialize parameters and flags
        """        
        self.continue_execution = True
        self.segway_battery_low = False
        self.rmp_issued_dyn_rsp = False
        self.using_amcl = rospy.get_param("~using_amcl", False)
        self.global_frame = rospy.get_param("~global_frame", 'odom')
        self.base_frame = rospy.get_param("~base_frame", 'segway/base_link')
        self.goal_timeout_sec = rospy.get_param("~goal_timeout_sec", 300)
        self.initial_state = rospy.get_param("~platform_mode", "tractor")
        
        """
        Goal state return values
        """
        self.goal_states = ['PENDING', 'ACTIVE', 'PREEMPTED', 
                       'SUCCEEDED', 'ABORTED', 'REJECTED',
                       'PREEMPTING', 'RECALLING', 'RECALLED',
                       'LOST']
                       
        """
        Variables to keep track of success rate, running time,
        and distance traveled
        """
        self.n_goals = 0
        self.n_successes = 0
        self.distance_traveled = 0
        self.start_time = rospy.Time.now()
        self.running_time = 0
        self.rmp_operational_state = 0
        initial_request_states = dict({"tractor":TRACTOR_REQUEST,"balance":TRACTOR_REQUEST})
        
        try:
            initial_mode_req = initial_request_states[self.initial_state]
        except:
            rospy.logerr("Initial mode not recognized it should be tractor or balance")
            self._shutdown()
            return
                
        """
        Initialize subscribers
        """
        rospy.Subscriber("/segway/feedback/aux_power", AuxPower, self._handle_low_aux_power)
        rospy.Subscriber("/segway/feedback/status", Status, self._handle_status)
        rospy.Subscriber("/segway/feedback/propulsion", Propulsion, self._handle_low_propulsion_power)
        rospy.Subscriber("/move_base_simple/goal", PoseStamped,  self._simple_goal_cb)
        rospy.Subscriber('/segway/teleop/abort_navigation',Bool,self._shutdown)
        rospy.Subscriber('/initialpose',PoseWithCovarianceStamped)
        self.simple_goal_pub = rospy.Publisher('/segway_move_base/goal', MoveBaseActionGoal, queue_size=10)
        self.new_goal = MoveBaseActionGoal()
        
        """
        Publishers to manually control the robot (e.g. to stop it) and send gp commands
        """
        self.config_cmd = ConfigCmd()
        self.cmd_config_cmd_pub = rospy.Publisher('/segway/gp_command', ConfigCmd, queue_size=10)
        self.cmd_vel_pub = rospy.Publisher('/segway/teleop/cmd_vel', Twist, queue_size=10)
            
        if (BALANCE_REQUEST == initial_mode_req):
            rospy.loginfo("Please put the platform into balance by tipping past 0 deg in pitch")
        
        if (False == self._goto_mode_and_indicate(initial_mode_req)):
            rospy.logerr("Could not set operational state")
            rospy.logerr("Platform did not respond")
            self._shutdown()
            return
        
        """
        Get the initial pose from the user
        """
        if (True == self.using_amcl):
            rospy.loginfo("*** Click the 2D Pose Estimate button in RViz to set the robot's initial pose...")
            rospy.wait_for_message('initialpose', PoseWithCovarianceStamped)
            
            my_cmd = Twist()
            my_cmd.angular.z = 1.0
            time_to_twist = rospy.Duration(5.0)
            start_time = rospy.Time.now()
            r = rospy.Rate(10)
            while (rospy.Time.now() - start_time) < time_to_twist:
                self.cmd_vel_pub.publish(my_cmd)
                r.sleep()
        
        self.last_pose = self._get_current_pose()
        
        if (None == self.last_pose):
            rospy.logerr('Could not get initial pose!!!! exiting....')
            self._shutdown()
            return
        
        """
        Subscribe to the move_base action server
        """
        self.move_base_client = actionlib.SimpleActionClient("move_base_navi", MoveBaseAction)
        rospy.loginfo("Waiting for move_base action server...move_base_navi")
        
        """
        Wait 60 seconds for the action server to become available
        """
        if (self.move_base_client.wait_for_server(rospy.Duration(60))):
            rospy.loginfo("Connected to move base server")
        else:
            rospy.logerr("Could not connect to action server")
            self._shutdown()
            return
        
        """
        Start the action server
        """
        self.action_ = MoveBaseAction()        
        self.move_base_server = actionlib.SimpleActionServer("segway_move_base", MoveBaseAction,execute_cb=self._execute_goal, auto_start = False)
        self.move_base_server.register_preempt_callback(self._preempt_cb)
        self.move_base_server.start()
        rospy.loginfo("Segway move base server started")


    def _execute_goal(self,goal):
                
        rospy.loginfo("Received a new goal")
        
        """
        See if the battery is low (threshold is 10% ABB reports at 5%)
        TODO: check FSW from embedded system
        """
        if self.segway_battery_low:
            rospy.loginfo("Dangerous to navigate with low Aux Power, Runtime Warning..... Plug me in for up to 1.5hrs for a full charge.")
            return
            
            
        """
        Send the goal; Allow user defined timeout to get there;Let the user know where the robot is going next
        """
        rospy.loginfo("Going to (X,Y): (%(1).3f,%(2).3f)"%{"1":goal.target_pose.pose.position.x,"2":goal.target_pose.pose.position.y})

        self.n_goals+=1
        self.goal_timeout = rospy.Duration(self.goal_timeout_sec)
        self.goal_start_time = rospy.Time.now()
        self.move_base_client.send_goal(goal,done_cb=self._done_moving_cb,feedback_cb=self._feedback_cb)
        delay = rospy.Duration(0.1)
        
        while not self.move_base_client.wait_for_result(delay) and not rospy.is_shutdown():
            """
            If the battery is low, we timed out, or got preempted stop moving
            """
            if self.segway_battery_low:
                self.move_base_client.cancel_goal()
                self.move_base_server.set_aborted(None, "Dangerous to navigate with low Aux Power, cancelling goal")
                rospy.loginfo("Dangerous to navigate with low Aux Power, Runtime Warning... Plug me in for up to 1.5hrs for a full charge..")
                return
            
            if self.rmp_issued_dyn_rsp:
                self.move_base_client.cancel_goal()
                self.move_base_server.set_aborted(None, "Platform initiated dynamic response")
                rospy.loginfo("Cannot navigate when platform is executing dynamic response")
                return
            
            if ((rospy.Time.now() - self.goal_start_time) > self.goal_timeout):
                self.move_base_client.cancel_goal()
                self.move_base_server.set_aborted(None, "Goal has timed out took longer than %f"%self.goal_timeout)
                rospy.loginfo("Timed out while trying to acheive new goal, cancelling move_base goal.")
                return
        
        """
        The goal should not be active at this point
        """
        assert not self.move_base_server.is_active()
        
    def _feedback_cb(self,feedback):
        self.move_base_server.publish_feedback(feedback)
        
    def _preempt_cb(self):
        self.move_base_client.cancel_goals_at_and_before_time(rospy.Time.now())
        rospy.logwarn("Current move base goal cancelled")
        if (self.move_base_server.is_active()):
            if not self.move_base_server.is_new_goal_available():
                rospy.loginfo("Preempt requested without new goal, cancelling move_base goal.")
                self.move_base_client.cancel_goal()

            self.move_base_server.set_preempted(MoveBaseResult(), "Got preempted by a new goal")
        
                
    def _done_moving_cb(self,status,result):

        if status == GoalStatus.SUCCEEDED:
            self.n_successes += 1
            self._moving = False
            self.move_base_server.set_succeeded(result, "Goal succeeded!")
        elif status == GoalStatus.ABORTED:
            self.move_base_server.set_aborted(result, "Failed to move, ABORTED")
            rospy.loginfo("Goal aborted with error code: " + str(self.goal_states[status])) 
        elif status != GoalStatus.PREEMPTED:
            self.move_base_server.set_aborted(result, "Unknown result from move_base")
            rospy.loginfo("Goal failed with error code: " + str(self.goal_states[status])) 
        
        
        new_pose = self._get_current_pose()
        self.distance_traveled += sqrt(pow(new_pose.pose.pose.position.x - 
                            self.last_pose.pose.pose.position.x, 2) +
                        pow(new_pose.pose.pose.position.y - 
                            self.last_pose.pose.pose.position.y, 2))
        self.last_pose = new_pose
        
        """
        How long have we been running?
        """
        self.running_time = rospy.Time.now() - self.start_time
        self.running_time = self.running_time.secs / 60.0
        
        """
        Print a summary success/failure, distance traveled and time elapsed
        """
        rospy.loginfo("Success so far: " + str(self.n_successes) + "/" + 
                      str(self.n_goals) + " = " + 
                      str(100 * self.n_successes/self.n_goals) + "%")
        rospy.loginfo("Running time: " + str(trunc(self.running_time, 1)) + 
                      " Total Distance: " + str(trunc(self.distance_traveled, 1)) + " m")
            
    def _simple_goal_cb(self, simple_goal):
        
        """
        Make sure the goal is in the global reference frame before adding it to the queue;
        sometimes the user can have the wrong frame selected in RVIZ for the fixed frame
        It should usually be /map or /odom depending on how the user is running the navigation stack
        """
        rospy.loginfo('got here-----------------------------------------------------------------------------')
        if (simple_goal.header.frame_id != self.global_frame) and (('/'+simple_goal.header.frame_id) != self.global_frame):
            rospy.logerr('MoveBaseSimpleGoal is not in correct frame!!!')
            rospy.logerr('expected global frame %(1)s but got %(2)s'%{'1':self.global_frame,'2':simple_goal.header.frame_id})
            return
        
        self.new_goal.goal.target_pose = simple_goal
        self.simple_goal_pub.publish(self.new_goal)

    def _handle_low_aux_power(self, battery_msg ):
        if (battery_msg.aux_soc[1] < 10.0):
            self.segway_battery_low = True
            
    def _handle_low_propulsion_power(self, propulsion_msg ):
        if (propulsion_msg.min_propulsion_battery_soc < 10.0):
            self.segway_battery_low = True

    def _handle_status(self,stat):
        if stat.dynamic_response != 0: 
            self.rmp_issued_dyn_rsp = True
        
        self.rmp_operational_state = stat.operational_state

    def _goto_mode_and_indicate(self,requested):        
        """
        define the commands for the function
        """
        config_cmd = ConfigCmd()
        
        """
        Send the audio command
        """
        r = rospy.Rate(10)
        start_time = rospy.Time.now().to_sec()
        while ((rospy.Time.now().to_sec() - start_time) < 30.0) and (RMP_MODES_DICT[requested] != self.rmp_operational_state):
            config_cmd.header.stamp = rospy.Time.now()
            config_cmd.gp_cmd = 'GENERAL_PURPOSE_CMD_SET_OPERATIONAL_MODE'
            config_cmd.gp_param = requested
            self.cmd_config_cmd_pub.publish(config_cmd)
            r.sleep()
        
        if (RMP_MODES_DICT[requested] != self.rmp_operational_state):
            rospy.logerr("Could not set operational Mode")
            rospy.loginfo("The platform did not respond, ")
            return False

        rospy.sleep(2)
        
        """
        Send the audio command
        """
        r = rospy.Rate(10)
        start_time = rospy.Time.now().to_sec()
        while ((rospy.Time.now().to_sec() - start_time) < 2.0):
            config_cmd.header.stamp = rospy.Time.now()
            config_cmd.gp_cmd = 'GENERAL_PURPOSE_CMD_SET_AUDIO_COMMAND'
            config_cmd.gp_param = RMP_MODES_AUDIO_DICT[requested]
            self.cmd_config_cmd_pub.publish(config_cmd)
            r.sleep()
            
    def _get_current_pose(self):

        """
        Gets the current pose of the base frame in the global frame
        """
        current_pose = None
        listener = tf.TransformListener()
        rospy.sleep(1.0)
        try:
            listener.waitForTransform(self.base_frame, self.global_frame, rospy.Time(), rospy.Duration(1.0))
        except:
            pass
        try:
            (trans,rot) = listener.lookupTransform(self.base_frame, self.global_frame, rospy.Time(0))
        
            pose_parts = [0.0] * 7
            pose_parts[0]  = trans[0]
            pose_parts[1]  = trans[1]
            pose_parts[2]  = 0.0
            euler = tf.transformations.euler_from_quaternion(rot)
            rot = tf.transformations.quaternion_from_euler(0,0,euler[2])
            pose_parts[3] = rot[0]
            pose_parts[4] = rot[1]
            pose_parts[5] = rot[2]
            pose_parts[6] = rot[3]       
        
            current_pose = PoseWithCovarianceStamped()
            current_pose.header.stamp = rospy.Time.now()
            current_pose.header.frame_id = self.global_frame
            current_pose.pose.pose = Pose(Point(pose_parts[0], pose_parts[1], pose_parts[2]), Quaternion(pose_parts[3],pose_parts[4],pose_parts[5],pose_parts[6])) 
        except:
            rospy.loginfo("Could not get transform from %(1)s->%(2)s"%{"1":self.global_frame,"2":self.base_frame})
            
        return current_pose
        
            
    def _shutdown(self):
        rospy.loginfo("Stopping the robot...")
        try:
            self.move_base_client.cancel_all_goals()
        except:
            pass
        
        try:        
            r = rospy.Rate(10)
            start_time = rospy.Time.now().to_sec()
            while ((rospy.Time.now().to_sec() - start_time) < 2.0):
                self.cmd_vel_pub.publish(Twist())
                r.sleep()
        except:
            pass
      
def trunc(f, n):
    """
    Truncates/pads a float f to n decimal places without rounding
    """
    slen = len('%.*f' % (n, f))
    return float(str(f)[:slen])
